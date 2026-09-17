"""Dialogues personnalisés du client.

`tkinter.messagebox` ne sait pas afficher de case à cocher. Or certaines
questions reviennent après CHAQUE bon (l'impression, par exemple) : un poste
qui n'imprime jamais subit alors un clic inutile toute la journée. Ce module
fournit la même question avec une case « Ne plus demander », dont la réponse
est mémorisée dans la configuration du poste.
"""
import customtkinter as ctk

from i18n import t


def demander_avec_memoire(parent, titre: str, message: str,
                          texte_case: str | None = None,
                          oui: str | None = None,
                          non: str | None = None) -> tuple[bool, bool]:
    """Question Oui/Non avec case « Ne plus demander ».

    Renvoie (réponse, ne_plus_demander). Fermer la fenêtre équivaut à « Non »
    sans mémoriser : refuser par accident ne doit pas couper la question
    définitivement.
    """
    # Valeurs par défaut résolues ici, pas dans la signature : une valeur
    # par défaut est évaluée à l'import, avant que la langue soit fixée.
    texte_case = t("Ne plus demander") if texte_case is None else texte_case
    oui = t("Oui") if oui is None else oui
    non = t("Non") if non is None else non

    resultat = {"reponse": False, "memoriser": False}

    fenetre = ctk.CTkToplevel(parent)
    fenetre.title(titre)
    fenetre.resizable(False, False)
    fenetre.transient(parent)

    cadre = ctk.CTkFrame(fenetre, fg_color="transparent")
    cadre.pack(padx=24, pady=20, fill="both", expand=True)

    ctk.CTkLabel(cadre, text=message, justify="left", wraplength=380).pack(
        anchor="w", pady=(0, 14))

    case = ctk.CTkCheckBox(cadre, text=texte_case)
    case.pack(anchor="w", pady=(0, 16))

    boutons = ctk.CTkFrame(cadre, fg_color="transparent")
    boutons.pack(anchor="e")

    def repondre(valeur: bool):
        resultat["reponse"] = valeur
        resultat["memoriser"] = bool(case.get())
        fenetre.destroy()

    ctk.CTkButton(boutons, text=non, width=100, fg_color="#6b7280",
                  hover_color="#4b5563",
                  command=lambda: repondre(False)).pack(side="right", padx=(8, 0))
    ctk.CTkButton(boutons, text=oui, width=100,
                  command=lambda: repondre(True)).pack(side="right")

    # La croix de fermeture vaut « Non », sans mémoriser.
    fenetre.protocol("WM_DELETE_WINDOW", lambda: repondre(False))

    fenetre.update_idletasks()
    _centrer(fenetre, parent)
    # grab_set après update : sur Windows la fenêtre doit être mappée pour
    # capter les événements, sinon l'appel échoue et le dialogue n'est pas modal.
    try:
        fenetre.grab_set()
    except Exception:
        pass
    fenetre.focus_force()
    parent.wait_window(fenetre)

    return resultat["reponse"], resultat["memoriser"]


def demander_quantite(parent, titre: str, message: str,
                      valeur_initiale: str = "", unite: str = "") -> str | None:
    """Petite boîte de saisie d'une quantité. Renvoie le texte, ou None.

    `tkinter.simpledialog.askfloat` refuserait « 12,5 » (virgule décimale du
    clavier de l'opérateur) et afficherait ses messages en anglais. On rend
    donc le TEXTE brut : c'est l'appelant qui valide, avec le même
    `float_saisie` que le reste des écrans, et qui affiche l'erreur en
    français à l'endroit habituel.

    La valeur initiale est présélectionnée : corriger « 40 » en « 4 » se fait
    en tapant le chiffre, sans effacer d'abord.
    """
    resultat = {"texte": None}

    fenetre = ctk.CTkToplevel(parent)
    fenetre.title(titre)
    fenetre.resizable(False, False)
    fenetre.transient(parent)

    cadre = ctk.CTkFrame(fenetre, fg_color="transparent")
    cadre.pack(padx=24, pady=20, fill="both", expand=True)

    ctk.CTkLabel(cadre, text=message, justify="left", wraplength=360).pack(
        anchor="w", pady=(0, 12))

    ligne_saisie = ctk.CTkFrame(cadre, fg_color="transparent")
    ligne_saisie.pack(anchor="w", pady=(0, 16))
    champ = ctk.CTkEntry(ligne_saisie, width=140)
    champ.pack(side="left")
    champ.insert(0, str(valeur_initiale))
    if unite:
        ctk.CTkLabel(ligne_saisie, text=unite, text_color="#9ca3af").pack(
            side="left", padx=(8, 0))

    boutons = ctk.CTkFrame(cadre, fg_color="transparent")
    boutons.pack(anchor="e")

    def valider(_event=None):
        resultat["texte"] = champ.get()
        fenetre.destroy()

    ctk.CTkButton(boutons, text=t("Annuler"), width=100, fg_color="#6b7280",
                  hover_color="#4b5563",
                  command=fenetre.destroy).pack(side="right", padx=(8, 0))
    ctk.CTkButton(boutons, text=t("Valider"), width=120,
                  command=valider).pack(side="right")

    champ.bind("<Return>", valider)
    champ.bind("<KP_Enter>", valider)
    # La croix de fermeture vaut « Annuler » : la ligne reste inchangée.
    fenetre.protocol("WM_DELETE_WINDOW", fenetre.destroy)

    fenetre.update_idletasks()
    _centrer(fenetre, parent)
    try:
        fenetre.grab_set()
    except Exception:
        pass
    champ.focus_force()
    try:
        champ.select_range(0, "end")
    except Exception:
        # `select_range` n'existe pas sur toutes les versions de CTkEntry :
        # le focus est l'essentiel, pas la présélection.
        pass
    parent.wait_window(fenetre)

    return resultat["texte"]


def mot_confirme(saisie, mot: str) -> bool:
    """Vrai si `saisie` reproduit `mot` (casse et espaces de bord ignorés).

    Fonction pure, séparée du dialogue, pour être vérifiable sans fenêtre Tk.
    La casse est ignorée : sur un clavier de poste, exiger les majuscules
    exactes ne rend l'opération ni plus réfléchie ni plus sûre — c'est le fait
    de devoir RECOPIER un mot qui empêche le clic réflexe.
    """
    if not isinstance(saisie, str):
        return False
    return saisie.strip().casefold() == (mot or "").strip().casefold()


def demander_mot_de_confirmation(parent, titre: str, message: str,
                                 mot: str) -> bool:
    """Confirmation par recopie d'un mot. Renvoie True seulement si exact.

    Deuxième verrou des opérations irréversibles : un « Oui / Non » se clique
    par réflexe, et un double-clic rapide sur le bouton d'origine peut même
    valider la boîte qui vient de s'ouvrir. Recopier un mot ne peut pas se
    faire par accident.

    Le bouton de validation reste désactivé tant que le mot n'est pas exact :
    l'opérateur voit tout de suite si sa saisie convient, plutôt que de se
    faire refuser après coup sans savoir pourquoi.
    """
    resultat = {"ok": False}

    fenetre = ctk.CTkToplevel(parent)
    fenetre.title(titre)
    fenetre.resizable(False, False)
    fenetre.transient(parent)

    cadre = ctk.CTkFrame(fenetre, fg_color="transparent")
    cadre.pack(padx=24, pady=20, fill="both", expand=True)

    ctk.CTkLabel(cadre, text=message, justify="left", wraplength=420).pack(
        anchor="w", pady=(0, 10))
    ctk.CTkLabel(
        cadre,
        text=t("Tape « {mot} » pour confirmer :").format(mot=mot),
        justify="left", text_color="#f59e0b").pack(anchor="w", pady=(0, 6))

    champ = ctk.CTkEntry(cadre, width=260)
    champ.pack(anchor="w", pady=(0, 16))

    boutons = ctk.CTkFrame(cadre, fg_color="transparent")
    boutons.pack(anchor="e")

    def valider():
        if not mot_confirme(champ.get(), mot):
            return
        resultat["ok"] = True
        fenetre.destroy()

    bouton_ok = ctk.CTkButton(boutons, text=t("Confirmer"), width=120,
                              fg_color="#b91c1c", hover_color="#991b1b",
                              state="disabled", command=valider)

    def _surveiller(*_args):
        try:
            bouton_ok.configure(
                state="normal" if mot_confirme(champ.get(), mot) else "disabled")
        except Exception:
            pass

    champ.bind("<KeyRelease>", _surveiller)
    champ.bind("<Return>", lambda _e: valider())

    ctk.CTkButton(boutons, text=t("Annuler"), width=100, fg_color="#6b7280",
                  hover_color="#4b5563",
                  command=fenetre.destroy).pack(side="right", padx=(8, 0))
    bouton_ok.pack(side="right")

    # La croix de fermeture vaut « Annuler » : sur une opération destructrice,
    # tout ce qui n'est pas une confirmation explicite est un refus.
    fenetre.protocol("WM_DELETE_WINDOW", fenetre.destroy)

    fenetre.update_idletasks()
    _centrer(fenetre, parent)
    try:
        fenetre.grab_set()
    except Exception:
        pass
    champ.focus_force()
    parent.wait_window(fenetre)

    return resultat["ok"]


def _centrer(fenetre, parent) -> None:
    try:
        x = parent.winfo_rootx() + (parent.winfo_width() - fenetre.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - fenetre.winfo_height()) // 3
        fenetre.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    except Exception:
        pass


def infobulle(widget, texte) -> None:
    """Attache une infobulle au survol.

    `texte` accepte une chaîne ou une fonction sans argument : le contenu est
    alors calculé au survol, ce qui évite d'afficher une valeur figée au moment
    où l'infobulle a été posée.

    Utilisé pour lever une ambiguïté d'affichage : le badge de file d'attente
    est répété à l'identique sur Réception, Expédition et Retour, ce qui laisse
    croire à trois fois plus de bons en attente qu'il n'y en a réellement.
    """
    etat = {"fenetre": None}

    def afficher(_event=None):
        if etat["fenetre"] is not None:
            return
        try:
            contenu = texte() if callable(texte) else texte
            if not contenu:
                return
            bulle = ctk.CTkToplevel(widget)
            bulle.wm_overrideredirect(True)
            bulle.attributes("-topmost", True)
            ctk.CTkLabel(bulle, text=contenu, fg_color="#1f2937", text_color="white",
                         corner_radius=6).pack(padx=8, pady=4)
            bulle.geometry(f"+{widget.winfo_rootx() + 20}+{widget.winfo_rooty() + 24}")
            etat["fenetre"] = bulle
        except Exception:
            etat["fenetre"] = None

    def masquer(_event=None):
        bulle = etat["fenetre"]
        etat["fenetre"] = None
        if bulle is not None:
            try:
                bulle.destroy()
            except Exception:
                pass

    widget.bind("<Enter>", afficher, add="+")
    widget.bind("<Leave>", masquer, add="+")
    widget.bind("<Destroy>", masquer, add="+")
