import contextlib
import threading

# Callback installé par l'application principale (client/main.py) pour
# rediriger vers l'écran de connexion quand une session expire.
#
# Pourquoi une variable de module plutôt qu'un monkeypatch de run_async :
# les vues font « from async_call import run_async » au moment de l'import,
# donc elles capturent la fonction telle qu'elle est à cet instant. Remplacer
# async_call.run_async après coup n'aurait aucun effet sur elles. Ici, la
# fonction lit la variable à chaque appel : l'installation tardive fonctionne
# quel que soit le style d'import de la vue.
gestionnaire_session_expiree = None


def run_async(widget, fn, on_success, on_error):
    """Exécute fn() dans un thread et rejoue le résultat dans la boucle Tk.

    Sans cela, un appel réseau bloquant exécuté depuis la boucle d'événements
    fige toute la fenêtre pendant la durée du timeout — l'opérateur ne peut
    même plus ouvrir les Paramètres pour corriger l'adresse du serveur.
    """
    def worker():
        try:
            result = fn()
        except Exception as e:
            _planifier(widget, lambda e=e: _rejouer_erreur(e, on_error, widget))
        else:
            _planifier(widget, lambda r=result: _rejouer_succes(r, on_success, widget))

    threading.Thread(target=worker, daemon=True).start()


def _planifier(widget, rappel):
    """Programme `rappel` sur la boucle Tk, sans jamais lever dans le thread.

    `widget.after()` sur un widget déjà détruit ne lève pas RuntimeError mais
    tkinter.TclError (« invalid command name .!frame... ») : l'ancien
    `except RuntimeError` ne l'attrapait donc pas, et l'exception remontait
    non traitée dans le thread de travail. On attrape ici tout ce qui peut
    sortir de la couche Tcl.
    """
    try:
        widget.after(0, rappel)
    except Exception:
        # Fenêtre (ou interpréteur Tk) déjà fermée : il n'y a plus personne
        # pour afficher le résultat, et ce n'est pas une erreur.
        pass


def _widget_vivant(widget) -> bool:
    """Vrai tant qu'on n'a pas la preuve que le widget a disparu.

    Le doute profite au callback : un objet sans `winfo_exists` (double de
    test, wrapper) est considéré vivant plutôt que muselé en silence. Seul un
    widget qui répond explicitement « je n'existe plus », ou dont
    l'interpréteur Tk est déjà mort (TclError), fait sauter le callback.
    """
    verif = getattr(widget, "winfo_exists", None)
    if verif is None:
        return True
    try:
        return bool(verif())
    except Exception:
        return False


def _rejouer_succes(resultat, on_success, widget):
    """Rejoue le succès sur la boucle Tk, si l'écran qui l'attend existe encore.

    Scénario corrigé : l'opérateur ouvre un dialogue (changement de rôle,
    fiche produit, contact), clique Enregistrer, puis ferme la fenêtre — ou se
    déconnecte, ce qui détruit toutes les vues (main.py `_on_login`) — pendant
    que la requête est encore en vol. La réponse arrivait ensuite dans un
    `on_success` qui appelait `.configure()` / `.destroy()` sur des widgets
    disparus : TclError levée à l'intérieur d'un callback Tk. En mode fenêtré
    (build PyInstaller), la trace part dans le vide et l'écran se fige à
    moitié rafraîchi.
    """
    if not _widget_vivant(widget):
        return
    try:
        on_success(resultat)
    except Exception:
        if _widget_vivant(widget):
            raise  # vraie erreur de la vue : ne pas la masquer


@contextlib.contextmanager
def _dialogues_muets():
    """Neutralise les boîtes de dialogue tkinter le temps d'un appel.

    Les vues font « from tkinter import messagebox » au niveau module : les
    fonctions sont donc résolues sur le module à chaque appel, et remplacer
    ses attributs suffit à museler toutes les vues d'un coup.
    """
    from tkinter import messagebox

    muets = {
        "showerror": lambda *a, **k: None,
        "showwarning": lambda *a, **k: None,
        "showinfo": lambda *a, **k: None,
        "askyesno": lambda *a, **k: False,
        "askokcancel": lambda *a, **k: False,
        "askretrycancel": lambda *a, **k: False,
        "askquestion": lambda *a, **k: "no",
    }
    anciens = {}
    for nom, remplacement in muets.items():
        if hasattr(messagebox, nom):
            anciens[nom] = getattr(messagebox, nom)
            setattr(messagebox, nom, remplacement)
    try:
        yield
    finally:
        for nom, original in anciens.items():
            setattr(messagebox, nom, original)


def _rejouer_erreur(exc, on_error, widget=None):
    """Aiguille l'erreur : session expirée -> retour au login, sinon la vue.

    Une session expirée n'est pas une erreur métier à afficher dans la vue,
    mais on_error DOIT quand même être appelé : chaque écran désactive son
    bouton avant l'appel réseau et ne le réactive que dans on_success ou
    on_error. Sauter on_error laissait le bouton grisé pour toujours, même
    après reconnexion (les vues déjà construites ne sont pas recréées).

    On appelle donc on_error avec les dialogues muselés — la vue réactive son
    bouton et remet son état à zéro, sans afficher de message par-dessus la
    redirection vers l'écran de connexion.
    """
    from api_client import SessionExpiredError

    if isinstance(exc, SessionExpiredError) and gestionnaire_session_expiree is not None:
        try:
            # La vue peut avoir disparu entre-temps ; la redirection, elle,
            # doit avoir lieu dans tous les cas.
            if widget is None or _widget_vivant(widget):
                with _dialogues_muets():
                    on_error(exc)
        except Exception:
            # Un échec du nettoyage de la vue ne doit jamais empêcher la
            # redirection : sans elle, l'opérateur reste bloqué.
            pass
        gestionnaire_session_expiree()
        return
    if widget is not None and not _widget_vivant(widget):
        # Plus d'écran pour afficher le message : le signaler reviendrait à
        # planter dans un `.configure()` sur un widget détruit.
        return
    on_error(exc)
