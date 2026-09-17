import tkinter as tk
import webbrowser

import customtkinter as ctk

import file_attente
import i18n
import reference_data
import version as client_version
from api_client import ApiClient
from i18n import t
from async_call import run_async
from config import load_config, save_config, server_base_url, timeouts, websocket_url
from websocket_client import WMSWebSocketClient
from views.dashboard import DashboardView
from views.document_form import DocumentFormView
from views.history import HistoryView
from views.indicateurs import IndicateursView
from views.inventory import InventoryView
from views.overview import OverviewView
from views.products import ProductsView
from views.qualite_referentiel import QualiteReferentielView
from views.thresholds import ThresholdsView
from views.users import UsersView
from views.contacts import ContactsView
from views.audit_log import AuditLogView
from views.bottles import BottlesView
from views.rl_returns import RlReturnsView
from views.alertes_regionales import AlertesRegionalesView
from views.entrepots_regionaux_admin import EntrepotsRegionauxAdminView
from views.etat_systeme import (
    EtatSystemeView, lignes_checklist_fermeture, lignes_checklist_ouverture,
)
from views.historique_regional import HistoriqueRegionalView
from views.inventaire_regional import InventaireRegionalView
from views.reception_regionale import ReceptionRegionaleView
from views.transferts_regionaux import TransfertsRegionauxView
from views.utils import erreur_mot_de_passe

ctk.set_appearance_mode("dark")
ctk.set_default_color_theme("blue")


class LoginDialog(ctk.CTkToplevel):
    """Fenêtre de connexion utilisateur."""

    def __init__(self, master, api, on_login):
        super().__init__(master)
        self.title(t("Connexion"))
        self.geometry("340x320")
        self.resizable(False, False)
        self.api = api
        self.on_login = on_login
        self.result = None
        self.master_app = master
        # (widget, texte_fr) : réappliqué par _appliquer_langue() sans
        # reconstruire la fenêtre — le choix se fait ICI, avant que le reste
        # de l'appli ne soit construit, donc rien d'autre à retraduire.
        self._textes: list[tuple] = []

        self.update_idletasks()
        x = master.winfo_x() + (master.winfo_width() // 2) - 170
        y = master.winfo_y() + (master.winfo_height() // 2) - 160
        self.geometry(f"340x320+{x}+{y}")

        self.transient(master)
        self.lift()
        self.attributes("-topmost", True)
        self.after(200, lambda: self.attributes("-topmost", False))
        self.focus_force()
        self.grab_set()

        # Choix de langue tout en haut, avant même l'identifiant : c'est la
        # toute première décision à l'entrée dans l'appli, pas un réglage
        # enfoui dans Paramètres qui exigerait de redémarrer.
        cadre_langue = ctk.CTkFrame(self, fg_color="transparent")
        cadre_langue.pack(fill="x", padx=16, pady=(12, 0))
        self._boutons_langue = {}
        # Les trois langues viennent de i18n : ajouter une langue là-bas suffit,
        # les deux sélecteurs (connexion et premier lancement) suivent.
        for code, libelle in i18n.LIBELLES_LANGUES.items():
            btn = ctk.CTkButton(cadre_langue, text=libelle, width=84, height=24,
                                font=ctk.CTkFont(size=11),
                                command=lambda c=code: self._appliquer_langue(c))
            btn.pack(side="right", padx=(4, 0))
            self._boutons_langue[code] = btn
        self._maj_boutons_langue()

        self._lbl_titre = ctk.CTkLabel(self, text=t("Connexion"),
                                       font=ctk.CTkFont(size=18, weight="bold"))
        self._lbl_titre.pack(pady=(12, 16))
        self._textes.append((self._lbl_titre, "Connexion"))

        self.user_var = tk.StringVar()
        self.pass_var = tk.StringVar()

        lbl_id = ctk.CTkLabel(self, text=t("Identifiant :"))
        lbl_id.pack(anchor="w", padx=30)
        self._textes.append((lbl_id, "Identifiant :"))
        user_entry = ctk.CTkEntry(self, textvariable=self.user_var, width=260)
        user_entry.pack(pady=(0, 8), padx=30)
        user_entry.bind("<Return>", lambda e: self._login())

        lbl_pass = ctk.CTkLabel(self, text=t("Mot de passe :"))
        lbl_pass.pack(anchor="w", padx=30)
        self._textes.append((lbl_pass, "Mot de passe :"))
        pass_entry = ctk.CTkEntry(self, textvariable=self.pass_var, width=260, show="*")
        pass_entry.pack(pady=(0, 12), padx=30)
        pass_entry.bind("<Return>", lambda e: self._login())

        self.status_label = ctk.CTkLabel(self, text="", text_color="orange")
        self.status_label.pack()

        self.bouton_connexion = ctk.CTkButton(self, text=t("Se connecter"), command=self._login)
        self.bouton_connexion.pack(pady=8)
        self._textes.append((self.bouton_connexion, "Se connecter"))

        # Le curseur est directement dans l'identifiant : l'opérateur tape sans
        # avoir à cliquer.
        user_entry.after(250, user_entry.focus_set)

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _maj_boutons_langue(self):
        courante = i18n.langue_courante()
        for code, btn in self._boutons_langue.items():
            btn.configure(fg_color="#2563eb" if code == courante else "#2a2d35",
                          hover_color="#1d4ed8" if code == courante else "#353840")

    def _appliquer_langue(self, code: str):
        """Change la langue tout de suite, sans redémarrage.

        Rien d'autre n'est encore construit à ce stade (aucune vue de l'appli
        principale) : mémoriser le choix ici suffit à ce que TOUT le reste,
        construit après la connexion, s'affiche déjà dans la bonne langue.
        """
        i18n.definir_langue(code)
        self.master_app.config_data["language"] = code
        save_config(self.master_app.config_data)
        self.title(t("Connexion"))
        for widget, texte_fr in self._textes:
            widget.configure(text=t(texte_fr))
        self._maj_boutons_langue()

    def _login(self):
        if self.bouton_connexion.cget("state") == "disabled":
            return
        username = self.user_var.get().strip()
        password = self.pass_var.get()
        if not username or not password:
            self.status_label.configure(text=t("Remplis les deux champs"))
            return
        # Appel réseau en tâche de fond : si le serveur ne répond pas, la
        # fenêtre reste utilisable au lieu de se figer pendant le timeout.
        self.bouton_connexion.configure(state="disabled", text=t("Connexion..."))
        self.status_label.configure(text=t("Connexion au serveur..."), text_color="gray")
        run_async(
            self,
            lambda: self.api.login(username, password),
            self._on_login_ok,
            self._on_login_erreur,
        )

    def _on_login_ok(self, result):
        self.result = result
        self.api.user_token = result["token"]
        self.api.username = result["user"]["username"]
        self.on_login(result)
        self.destroy()

    def _on_login_erreur(self, e):
        try:
            self.bouton_connexion.configure(state="normal", text=t("Se connecter"))
        except Exception:
            return  # fenêtre déjà fermée
        message = str(e)
        # Une panne réseau tient en plusieurs lignes (adresse utilisée, quoi
        # vérifier) : elles ne rentrent pas dans le bandeau d'une fenêtre de
        # connexion haute de 320 pixels et non redimensionnable, où elles
        # débordaient hors de l'écran. C'est pourtant L'erreur du premier
        # lancement sur un poste régional distant, celle qu'il faut pouvoir
        # lire en entier. On la sort donc dans une boîte de dialogue et on ne
        # garde dans le bandeau que la première ligne.
        if "contacter le serveur" in message.lower():
            try:
                self.status_label.configure(
                    text=t("Serveur injoignable"), text_color="orange")
            except Exception:
                pass
            from tkinter import messagebox
            messagebox.showerror(t("Serveur injoignable"), message, parent=self)
            return
        try:
            self.status_label.configure(text=message, text_color="orange")
        except Exception:
            pass

    def _on_close(self):
        if self.result is None:
            self.master.destroy()


class SetupAdminDialog(ctk.CTkToplevel):
    """Création du premier administrateur (premier lancement)."""

    def __init__(self, master, api, on_done):
        super().__init__(master)
        self.title(t("Créer le compte administrateur"))
        self.geometry("380x400")
        self.resizable(False, False)
        self.api = api
        self.on_done = on_done
        self.master_app = master
        self._textes: list[tuple] = []

        self.update_idletasks()
        x = master.winfo_x() + (master.winfo_width() // 2) - 190
        y = master.winfo_y() + (master.winfo_height() // 2) - 200
        self.geometry(f"380x400+{x}+{y}")

        self.transient(master)
        self.lift()
        self.attributes("-topmost", True)
        self.after(200, lambda: self.attributes("-topmost", False))
        self.focus_force()
        self.grab_set()

        cadre_langue = ctk.CTkFrame(self, fg_color="transparent")
        cadre_langue.pack(fill="x", padx=16, pady=(12, 0))
        self._boutons_langue = {}
        # Les trois langues viennent de i18n : ajouter une langue là-bas suffit,
        # les deux sélecteurs (connexion et premier lancement) suivent.
        for code, libelle in i18n.LIBELLES_LANGUES.items():
            btn = ctk.CTkButton(cadre_langue, text=libelle, width=84, height=24,
                                font=ctk.CTkFont(size=11),
                                command=lambda c=code: self._appliquer_langue(c))
            btn.pack(side="right", padx=(4, 0))
            self._boutons_langue[code] = btn
        self._maj_boutons_langue()

        lbl_titre = ctk.CTkLabel(self, text=t("Premier lancement"),
                                 font=ctk.CTkFont(size=16, weight="bold"))
        lbl_titre.pack(pady=(12, 4))
        self._textes.append((lbl_titre, "Premier lancement"))
        lbl_sous = ctk.CTkLabel(self, text=t("Crée le compte administrateur."),
                                text_color="gray")
        lbl_sous.pack(pady=(0, 12))
        self._textes.append((lbl_sous, "Crée le compte administrateur."))

        self.name_var = tk.StringVar()
        self.user_var = tk.StringVar()
        self.pass_var = tk.StringVar()

        for label, var, show in (("Nom complet :", self.name_var, None),
                                  ("Identifiant :", self.user_var, None),
                                  ("Mot de passe :", self.pass_var, "*")):
            lbl = ctk.CTkLabel(self, text=t(label))
            lbl.pack(anchor="w", padx=30)
            self._textes.append((lbl, label))
            ctk.CTkEntry(self, textvariable=var, width=300, show=show or "").pack(pady=(0, 8), padx=30)

        self.status_label = ctk.CTkLabel(self, text="", text_color="orange")
        self.status_label.pack()

        self.create_btn = ctk.CTkButton(self, text=t("Créer et se connecter"), command=self._create)
        self.create_btn.pack(pady=10)
        self._textes.append((self.create_btn, "Créer et se connecter"))

        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _maj_boutons_langue(self):
        courante = i18n.langue_courante()
        for code, btn in self._boutons_langue.items():
            btn.configure(fg_color="#2563eb" if code == courante else "#2a2d35",
                          hover_color="#1d4ed8" if code == courante else "#353840")

    def _appliquer_langue(self, code: str):
        i18n.definir_langue(code)
        self.master_app.config_data["language"] = code
        save_config(self.master_app.config_data)
        self.title(t("Créer le compte administrateur"))
        for widget, texte_fr in self._textes:
            widget.configure(text=t(texte_fr))
        self._maj_boutons_langue()

    def _create(self):
        name = self.name_var.get().strip()
        username = self.user_var.get().strip()
        password = self.pass_var.get()
        if not name or not username:
            self.status_label.configure(text=t("Tous les champs sont requis (mot de passe 12+ car.)"))
            return
        # Ce tout premier compte est un ADMINISTRATEUR : politique renforcée.
        erreur = erreur_mot_de_passe(password, "admin", username=username)
        if erreur:
            self.status_label.configure(text=t(erreur), text_color="orange")
            return
        self.create_btn.configure(state="disabled", text=t("Création en cours..."))
        self.status_label.configure(text=t("Connexion au serveur..."), text_color="gray")

        def _do_create():
            self.api.create_user(username, password, name, role="admin")
            return self.api.login(username, password)

        run_async(self, _do_create, self._on_create_ok, self._on_create_error)

    def _on_create_ok(self, result):
        self.api.user_token = result["token"]
        self.api.username = result["user"]["username"]
        self.on_done(result)
        self.destroy()

    def _on_create_error(self, e):
        self.create_btn.configure(state="normal", text=t("Créer et se connecter"))
        self.status_label.configure(text=str(e), text_color="orange")

    def _on_close(self):
        self.master.destroy()


class SettingsDialog(ctk.CTkToplevel):
    def __init__(self, master, config: dict, on_save):
        super().__init__(master)
        self.title(t("Connexion au serveur"))
        self.geometry("380x500")
        self.resizable(False, False)
        self.on_save = on_save
        self.config_data = config

        self.update_idletasks()
        x = master.winfo_x() + (master.winfo_width() // 2) - 190
        y = master.winfo_y() + (master.winfo_height() // 2) - 250
        self.geometry(f"380x500+{x}+{y}")

        self.transient(master)
        self.lift()
        self.attributes("-topmost", True)
        self.after(200, lambda: self.attributes("-topmost", False))
        self.focus_force()
        self.grab_set()

        ctk.CTkLabel(self, text=t("Configuration serveur"), font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(20, 10))

        self.host_var = tk.StringVar(value=config["server_host"])
        self.port_var = tk.StringVar(value=str(config["server_port"]))
        self.operator_var = tk.StringVar(value=config["operator"])

        for label, var in (("Adresse du serveur :", self.host_var),
                            ("Port :", self.port_var),
                            ("Ton nom (opérateur) :", self.operator_var)):
            ctk.CTkLabel(self, text=t(label)).pack(anchor="w", padx=30)
            ctk.CTkEntry(self, textvariable=var, width=300).pack(pady=(0, 10), padx=30)
            if var is self.host_var:
                # Le champ accepte aussi une URL complète (« https://… »), seul
                # moyen d'atteindre le serveur derrière un proxy sécurisé —
                # l'ancien libellé « Adresse IP » laissait croire le contraire.
                # Le port est alors ignoré, ce qu'il faut dire ici : sans cela
                # un opérateur croit s'être trompé de port et modifie au hasard.
                ctk.CTkLabel(
                    self,
                    text=t("Fournie par l'entrepôt central. Exemples :\n"
                           "192.168.1.10   ou   https://wms.exemple.com\n"
                           "(avec une adresse https, le port est ignoré)"),
                    text_color="gray", font=ctk.CTkFont(size=10),
                    justify="left",
                ).pack(anchor="w", padx=30, pady=(0, 8))

        # Permet de RÉACTIVER la question d'impression coupée par la case
        # « Ne plus demander » du dialogue de fin de bon.
        self.impression_var = tk.BooleanVar(
            value=bool(config.get("proposer_impression", True)))
        ctk.CTkCheckBox(self, text=t("Proposer d'imprimer après chaque bon"),
                        variable=self.impression_var).pack(anchor="w", padx=30, pady=(4, 0))

        ctk.CTkButton(self, text=t("Enregistrer"), command=self._save).pack(pady=14)

    def _save(self):
        self.config_data["proposer_impression"] = bool(self.impression_var.get())
        self.config_data["server_host"] = self.host_var.get().strip() or "127.0.0.1"
        try:
            self.config_data["server_port"] = int(self.port_var.get().strip() or 8000)
        except ValueError:
            self.config_data["server_port"] = 8000
        self.config_data["operator"] = self.operator_var.get().strip()
        save_config(self.config_data)
        self.on_save(self.config_data)
        self.destroy()


class ChangeMyPasswordDialog(ctk.CTkToplevel):
    def __init__(self, master, api, role: str = "", username: str = ""):
        super().__init__(master)
        self.api = api
        # Le rôle conditionne la politique de mot de passe : un admin doit en
        # saisir un plus solide. Défaut vide = règle générale, jamais un
        # blocage à tort si l'appelant ne le passe pas.
        self.role = role or ""
        # Sert au seul contrôle « le mot de passe ne contient pas
        # l'identifiant ». Vide = contrôle sauté, jamais un refus à tort.
        self.username = username or ""
        self.title(t("Changer mon mot de passe"))
        self.geometry("340x250")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        ctk.CTkLabel(self, text=t("Changer mon mot de passe"),
                     font=ctk.CTkFont(size=14, weight="bold")).pack(pady=(16, 12))

        self.old_var = tk.StringVar()
        self.new_var = tk.StringVar()
        self.confirm_var = tk.StringVar()

        ctk.CTkLabel(self, text=t("Mot de passe actuel :")).pack(anchor="w", padx=30)
        ctk.CTkEntry(self, textvariable=self.old_var, width=260, show="*").pack(padx=30, pady=(0, 6))
        ctk.CTkLabel(self, text=t("Nouveau mot de passe :")).pack(anchor="w", padx=30)
        ctk.CTkEntry(self, textvariable=self.new_var, width=260, show="*").pack(padx=30, pady=(0, 6))
        ctk.CTkLabel(self, text=t("Confirmer :")).pack(anchor="w", padx=30)
        ctk.CTkEntry(self, textvariable=self.confirm_var, width=260, show="*").pack(padx=30, pady=(0, 6))

        self.status = ctk.CTkLabel(self, text="", text_color="orange")
        self.status.pack()
        self.btn_save = ctk.CTkButton(self, text=t("Changer"), width=120, command=self._save)
        self.btn_save.pack(pady=8)

    def _save(self):
        old = self.old_var.get()
        new = self.new_var.get()
        confirm = self.confirm_var.get()
        if not old:
            self.status.configure(text=t("Mot de passe actuel requis"))
            return
        erreur = erreur_mot_de_passe(new, self.role, username=self.username)
        if erreur:
            self.status.configure(text=t(erreur))
            return
        if new != confirm:
            self.status.configure(text=t("Les mots de passe ne correspondent pas"))
            return

        # Appel réseau via run_async : exécuté directement, il gèle la fenêtre
        # le temps du timeout et l'opérateur croit l'application plantée.
        # Le bouton est verrouillé pour éviter un double envoi.
        self.btn_save.configure(state="disabled")
        self.status.configure(text=t("Envoi en cours…"), text_color="orange")
        run_async(
            self,
            lambda: self.api.change_my_password(old, new),
            self._on_save_ok,
            self._on_save_error,
        )

    def _on_save_ok(self, _resultat):
        if not self.winfo_exists():
            return
        from tkinter import messagebox
        messagebox.showinfo(t("OK"), t("Mot de passe changé avec succès"), parent=self)
        self.destroy()

    def _on_save_error(self, e):
        if not self.winfo_exists():
            return
        self.btn_save.configure(state="normal")
        self.status.configure(text=str(e), text_color="orange")


class App(ctk.CTk):
    def __init__(self):
        super().__init__()

        # La langue est fixée AVANT toute construction d'interface : les vues
        # sont bâties une fois puis mises en cache, un changement à chaud ne
        # retraduirait pas les écrans déjà construits.
        self.config_data = load_config()
        i18n.definir_langue(self.config_data.get("language", "fr"))

        self.title(t("Gestion d'entrepôt"))
        self.geometry("1100x650")
        try:
            self.state("zoomed")
        except tk.TclError:
            pass

        self.api = ApiClient(server_base_url(self.config_data),
                             timeout=timeouts(self.config_data))
        self._user_info = None
        self._nb_ecarts_stock = 0
        self._websocket_client = None

        reference_data.charger_depuis_serveur(self.api)

        self._build_layout()
        self._bind_shortcuts()
        # Les vues importent run_async au niveau module : un monkeypatch de
        # async_call.run_async ne les toucherait pas. On installe donc le
        # gestionnaire dans une variable lue à chaque erreur.
        import async_call
        async_call.gestionnaire_session_expiree = self._session_expiree

        self.protocol("WM_DELETE_WINDOW", self._on_fermeture)
        self.after(300, self._verifier_utilisateurs)

        # Rejeu de la file d'attente au niveau de l'application : l'opérateur
        # qui reste sur Réception ne voit jamais le tableau de bord, et ses
        # bons hors ligne resteraient bloqués indéfiniment.
        self._rejeu_en_cours = False
        self.after(8000, self._rejeu_automatique)
        # Le client WebSocket est démarré après la connexion réussie
        # (_on_login) : avant, aucun token n'est disponible et le serveur
        # refuserait la connexion.

    # -- Couleurs du thème redesign --
    SIDEBAR_BG = "#1a1d23"
    SIDEBAR_HOVER = "#252830"
    SIDEBAR_ACTIVE_BG = "#1e3a5f"
    SIDEBAR_ACTIVE_TEXT = "#60a5fa"
    SIDEBAR_TEXT = "#9ca3af"
    SIDEBAR_GROUP = "#6b7280"
    MAIN_BG = "#111318"

    NAV_ITEMS = [
        ("OPÉRATIONS", None),
        ("Tableau de bord", "📊"),
        ("Stock", "📦"),
        ("MOUVEMENTS", None),
        ("Réception", "🚚"),
        ("Expédition", "📤"),
        ("Retour", "🔄"),
        ("Inventaire", "📋"),
        ("Bouteilles", "🍾"),
        ("Retours équipement", "📡"),
        ("DONNÉES", None),
        ("Historique", "🕐"),
        ("Produits", "🗄"),
        ("Contacts", "📇"),
        ("Seuils", "🔒"),
        ("ADMINISTRATION", None),
        ("Indicateurs", "📈"),
        ("Entrepôts régionaux", "🏬"),
        ("Qualité référentiel", "🩺"),
        ("Utilisateurs", "👥"),
        ("Journal d'audit", "📝"),
        ("État du système", "🩹"),
        # Réservés au rôle « regional » : un compte régional ne voit QUE ces
        # quatre entrées, et aucun autre rôle ne les voit.
        ("MA RÉGION", None),
        ("Réception régionale", "📥"),
        ("Transfert entre régions", "🔁"),
        ("Inventaire régional", "📋"),
        ("Alertes régionales", "⚠"),
        ("Historique régional", "🕘"),
    ]

    def _build_layout(self):
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        sidebar = ctk.CTkFrame(self, width=220, corner_radius=0, fg_color=self.SIDEBAR_BG)
        sidebar.grid(row=0, column=0, sticky="nsw")
        sidebar.grid_propagate(False)
        self._sidebar = sidebar

        header = ctk.CTkFrame(sidebar, fg_color="transparent")
        header.pack(fill="x", padx=14, pady=(18, 14))

        logo_frame = ctk.CTkFrame(header, width=38, height=38, corner_radius=8, fg_color="#2563eb")
        logo_frame.pack(side="left")
        logo_frame.pack_propagate(False)
        ctk.CTkLabel(logo_frame, text="🏭", font=ctk.CTkFont(size=18)).place(relx=0.5, rely=0.5, anchor="center")

        title_frame = ctk.CTkFrame(header, fg_color="transparent")
        title_frame.pack(side="left", padx=(10, 0))
        # Le secteur (Consumables, FON...) est ajouté après connexion : sans
        # lui, rien à l'écran ne dit dans quel catalogue on se trouve, alors
        # que la même appli sert plusieurs secteurs sur ce poste.
        self._titre_app_label = ctk.CTkLabel(title_frame, text=t("WMS Entrepôt"),
                     font=ctk.CTkFont(size=14, weight="bold"), text_color="#f0f0f0")
        self._titre_app_label.pack(anchor="w")
        station = self.config_data.get("operator", "") or t("Poste local")
        self._station_label = ctk.CTkLabel(title_frame, text=station,
                                            font=ctk.CTkFont(size=10), text_color="#6b7280")
        self._station_label.pack(anchor="w")

        sep = ctk.CTkFrame(sidebar, height=1, fg_color="#2a2d35")
        sep.pack(fill="x", padx=14, pady=(0, 6))

        # Défilante : avec autant d'entrées (Bouteilles, Contacts, Journal
        # d'audit, etc.), une liste figée poussait Paramètres/Déconnexion
        # hors de l'écran sur les fenêtres moins hautes.
        nav_scroll = ctk.CTkScrollableFrame(
            sidebar, fg_color="transparent",
            scrollbar_button_color=self.SIDEBAR_BG,
            scrollbar_button_hover_color=self.SIDEBAR_HOVER)
        nav_scroll.pack(fill="both", expand=True, padx=2)

        self.content = ctk.CTkFrame(self, fg_color=self.MAIN_BG, corner_radius=0)
        self.content.grid(row=0, column=1, sticky="nsew")

        qc = self._maj_badge_file

        self.views = {
            "Tableau de bord": lambda parent: OverviewView(
                parent, self.api, get_ecarts=lambda: self._nb_ecarts_stock,
                sector=self._user_info.get("sector") if self._user_info else None),
            "Stock": lambda parent: DashboardView(parent, self.api, on_queue_change=qc,
                                                    on_alerte_change=self._maj_badge_alertes),
            "Réception": lambda parent: DocumentFormView(parent, self.api, "RECEIVING", self.config_data["operator"], on_queue_change=qc,
                                                         sector=self._user_info.get("sector") if self._user_info else None,
                                                         demo_mode=bool(self._user_info and self._user_info.get("demo_mode"))),
            "Expédition": lambda parent: DocumentFormView(parent, self.api, "DELIVERY", self.config_data["operator"], on_queue_change=qc,
                                                          sector=self._user_info.get("sector") if self._user_info else None,
                                                          demo_mode=bool(self._user_info and self._user_info.get("demo_mode"))),
            "Retour": lambda parent: DocumentFormView(parent, self.api, "RETURN", self.config_data["operator"], on_queue_change=qc,
                                                       sector=self._user_info.get("sector") if self._user_info else None,
                                                       demo_mode=bool(self._user_info and self._user_info.get("demo_mode"))),
            "Inventaire": lambda parent: InventoryView(parent, self.api, self.config_data["operator"], on_queue_change=qc,
                                                       sector=self._user_info.get("sector") if self._user_info else None,
                                                       role=self._user_info.get("role") if self._user_info else None),
            "Bouteilles": lambda parent: BottlesView(parent, self.api,
                                                         user_role=self._user_info["role"] if self._user_info else "operateur"),
            "Retours équipement": lambda parent: RlReturnsView(parent, self.api,
                                                         user_role=self._user_info["role"] if self._user_info else "operateur"),
            "Historique": lambda parent: HistoryView(
                parent, self.api,
                user_role=self._user_info["role"] if self._user_info else None,
                sector=self._user_info.get("sector") if self._user_info else None),
            "Produits": lambda parent: ProductsView(parent, self.api,
                                                     user_role=self._user_info["role"] if self._user_info else None),
            "Contacts": lambda parent: ContactsView(parent, self.api,
                                                     user_role=self._user_info["role"] if self._user_info else None),
            "Seuils": lambda parent: ThresholdsView(parent, self.api),
            "Indicateurs": lambda parent: IndicateursView(
                parent, self.api,
                user_role=self._user_info["role"] if self._user_info else None,
                sector=self._user_info.get("sector") if self._user_info else None),
            "Entrepôts régionaux": lambda parent: EntrepotsRegionauxAdminView(
                parent, self.api, on_alerte_change=self._rafraichir_badge_alertes_regionales),
            "Qualité référentiel": lambda parent: QualiteReferentielView(
                parent, self.api,
                user_role=self._user_info["role"] if self._user_info else None),
            "Utilisateurs": lambda parent: UsersView(parent, self.api),
            "Journal d'audit": lambda parent: AuditLogView(parent, self.api),
            "État du système": lambda parent: EtatSystemeView(
                parent, self.api,
                user_role=self._user_info["role"] if self._user_info else None),
            "Réception régionale": lambda parent: ReceptionRegionaleView(
                parent, self.api, region=self._region_utilisateur(),
                user_role=self._user_info["role"] if self._user_info else None),
            "Transfert entre régions": lambda parent: TransfertsRegionauxView(
                parent, self.api, region=self._region_utilisateur(),
                user_role=self._user_info["role"] if self._user_info else None,
                regions_possibles=self._regions_avec_entrepot(),
                sector=self._user_info.get("sector") if self._user_info else None),
            "Inventaire régional": lambda parent: InventaireRegionalView(
                parent, self.api, region=self._region_utilisateur(),
                user_role=self._user_info["role"] if self._user_info else None,
                sector=self._user_info.get("sector") if self._user_info else None),
            "Alertes régionales": lambda parent: AlertesRegionalesView(
                parent, self.api, region=self._region_utilisateur(),
                user_role=self._user_info["role"] if self._user_info else None),
            "Historique régional": lambda parent: HistoriqueRegionalView(
                parent, self.api, region=self._region_utilisateur(),
                user_role=self._user_info["role"] if self._user_info else None),
        }
        self.view_instances = {}
        self.nav_buttons = {}
        self._badge_labels = {}
        self._group_labels = {}

        for item_name, icon in self.NAV_ITEMS:
            if icon is None:
                # Empilé puis réordonné par _repack_nav() juste après la
                # boucle : seule sa position finale dans NAV_ITEMS compte.
                lbl = ctk.CTkLabel(nav_scroll, text=t(item_name),
                                   font=ctk.CTkFont(size=10), text_color=self.SIDEBAR_GROUP)
                lbl.pack(anchor="w", padx=10, pady=(12, 2))
                self._group_labels[item_name] = lbl
                continue

            btn_frame = ctk.CTkFrame(nav_scroll, fg_color="transparent", corner_radius=6, height=34)
            btn_frame.pack(fill="x", pady=1)
            btn_frame.pack_propagate(False)

            lbl_icon = ctk.CTkLabel(btn_frame, text=icon, font=ctk.CTkFont(size=14), width=28)
            lbl_icon.pack(side="left", padx=(8, 0))

            lbl_text = ctk.CTkLabel(btn_frame, text=t(item_name),
                                    font=ctk.CTkFont(size=12), text_color=self.SIDEBAR_TEXT, anchor="w")
            lbl_text.pack(side="left", padx=(4, 0), fill="x", expand=True)

            badge_lbl = ctk.CTkLabel(btn_frame, text="", font=ctk.CTkFont(size=9, weight="bold"),
                                     text_color="white", fg_color="#ef4444", corner_radius=8,
                                     width=0, height=18)
            self._badge_labels[item_name] = badge_lbl

            for widget in (btn_frame, lbl_icon, lbl_text):
                widget.bind("<Button-1>", lambda e, n=item_name: self._show(n))
                widget.bind("<Enter>", lambda e, f=btn_frame: f.configure(
                    fg_color=self.SIDEBAR_ACTIVE_BG if f == self._active_nav else self.SIDEBAR_HOVER))
                widget.bind("<Leave>", lambda e, f=btn_frame: f.configure(
                    fg_color=self.SIDEBAR_ACTIVE_BG if f == self._active_nav else "transparent"))

            self.nav_buttons[item_name] = (btn_frame, lbl_text, lbl_icon)

        # Rôle inconnu avant connexion : _repack_nav masque déjà les entrées
        # réservées à l'admin et le groupe ADMINISTRATION dans ce cas.
        self._repack_nav(role=None)

        self._active_nav = None

        footer = ctk.CTkFrame(sidebar, fg_color="transparent")
        footer.pack(side="bottom", fill="x", padx=8, pady=(4, 8))

        sep2 = ctk.CTkFrame(sidebar, height=1, fg_color="#2a2d35")
        sep2.pack(side="bottom", fill="x", padx=14)

        self.badge_file = ctk.CTkLabel(sidebar, text="", text_color="orange",
                                       font=ctk.CTkFont(size=11), cursor="hand2")
        self.badge_file.pack(side="bottom", padx=12, pady=(0, 4))
        self.badge_file.bind("<Button-1>", self._afficher_details_file)

        # Discret : le polling de secours (30s) prend le relais en silence, et
        # ce serait normalement invisible pour l'opérateur. Sans cet
        # indicateur, un poste peut passer des heures à croire que le temps
        # réel fonctionne alors que la reconnexion a été abandonnée.
        self._indicateur_temps_reel = ctk.CTkLabel(
            sidebar, text=t("⚠  Temps réel interrompu — cliquer pour relancer"),
            text_color="#f59e0b", font=ctk.CTkFont(size=10), cursor="hand2")
        self._indicateur_temps_reel.bind("<Button-1>", lambda e: self._relancer_websocket())

        self.btn_deconnexion = ctk.CTkButton(
            footer, text=t("⏻  Déconnexion"), anchor="w", fg_color="#7a2a2a",
            hover_color="#943535", height=30, font=ctk.CTkFont(size=11),
            corner_radius=6, command=self._deconnecter)

        self.btn_mdp = ctk.CTkButton(
            footer, text=t("🔑  Mon mot de passe"), anchor="w", fg_color="#2a2d35",
            hover_color="#353840", height=30, font=ctk.CTkFont(size=11),
            corner_radius=6, command=self._changer_mon_mdp)

        self.btn_parametres = ctk.CTkButton(
            footer, text=t("⚙  Paramètres"), anchor="w", fg_color="#2a2d35",
            hover_color="#353840", height=30, font=ctk.CTkFont(size=11),
            corner_radius=6, command=self._open_settings)

        self.user_frame = ctk.CTkFrame(footer, fg_color="#1e2028", corner_radius=8)
        self._user_avatar = ctk.CTkLabel(self.user_frame, text="", width=32, height=32,
                                         corner_radius=16, fg_color="#2563eb",
                                         font=ctk.CTkFont(size=12, weight="bold"), text_color="white")
        self._user_avatar.pack(side="left", padx=(8, 0), pady=6)
        self._user_name_frame = ctk.CTkFrame(self.user_frame, fg_color="transparent")
        self._user_name_frame.pack(side="left", padx=(8, 8), pady=6)
        self.user_label = ctk.CTkLabel(self._user_name_frame, text="", text_color="#d1d5db",
                                       font=ctk.CTkFont(size=11, weight="bold"))
        self.user_label.pack(anchor="w")
        self._user_role_label = ctk.CTkLabel(self._user_name_frame, text="", text_color="#6b7280",
                                              font=ctk.CTkFont(size=9))
        self._user_role_label.pack(anchor="w")

        self._repack_footer()
        self._maj_badge_file()
        # Pas de _show() ici : avant la connexion, self.api n'a pas de jeton.
        # Les vues (Tableau de bord, Stock) chargent leurs données dès leur
        # premier affichage — un appel ici partirait sans jeton, recevrait un
        # 401, et serait pris à tort pour une session expirée (déconnexion +
        # ouverture d'une seconde fenêtre de connexion en plus de celle du
        # login normal). _on_login() affiche le tableau de bord une fois
        # authentifié ; d'ici là, la zone de contenu reste vide derrière la
        # boîte de connexion.

    def _show(self, name: str):
        if name not in self.views:
            return
        # Les raccourcis clavier contournent la barre latérale : sans ce
        # garde-fou, Ctrl+3 ouvrirait la Réception centrale à un compte
        # régional, qui ne recevrait que des erreurs 403 du serveur.
        role = (self._user_info or {}).get("role")
        if (role == "regional") != (name in self.ONGLETS_REGIONAL):
            return
        for frame in self.view_instances.values():
            frame.pack_forget()

        if name not in self.view_instances:
            self.view_instances[name] = self.views[name](self.content)
        frame = self.view_instances[name]
        frame.pack(fill="both", expand=True)

        if hasattr(frame, "refresh_products"):
            frame.refresh_products()
        if hasattr(frame, "actualiser_date"):
            frame.actualiser_date()
        # Le chargement des données se fait ICI, après le `pack()` : une vue
        # tout juste construite n'est pas encore mappée, et son polling
        # interne refuse de charger tant qu'elle ne l'est pas.
        if hasattr(frame, "refresh") and name in ("Tableau de bord", "Stock",
                                              "Historique", "Seuils", "Utilisateurs", "Contacts",
                                              "Journal d'audit", "Bouteilles", "Entrepôts régionaux",
                                              "Qualité référentiel", "Indicateurs",
                                              "État du système",
                                              *self.ONGLETS_REGIONAL):
            frame.refresh()

        for n, widgets in self.nav_buttons.items():
            btn_frame, lbl_text, lbl_icon = widgets
            if n == name:
                btn_frame.configure(fg_color=self.SIDEBAR_ACTIVE_BG)
                lbl_text.configure(text_color=self.SIDEBAR_ACTIVE_TEXT)
                self._active_nav = btn_frame
            else:
                btn_frame.configure(fg_color="transparent")
                lbl_text.configure(text_color=self.SIDEBAR_TEXT)

    def dupliquer_bon(self, bon_data: dict):
        """Duplique un bon : navigue vers le formulaire et pre-remplit les lignes."""
        doc_type = bon_data.get("doc_type")
        nav_map = {
            "RECEIVING": "Réception",
            "DELIVERY": "Expédition",
            "RETURN": "Retour",
        }
        view_name = nav_map.get(doc_type)
        if not view_name:
            return
        # Detruire l'instance existante pour repartir d'un formulaire vierge
        if view_name in self.view_instances:
            self.view_instances[view_name].destroy()
            del self.view_instances[view_name]
        self._show(view_name)
        frame = self.view_instances.get(view_name)
        if frame and hasattr(frame, "pre_remplir"):
            frame.pre_remplir(bon_data)

    def _current_view_name(self) -> str | None:
        for name, frame in self.view_instances.items():
            if frame.winfo_ismapped():
                return name
        return None

    def _bind_shortcuts(self):
        self.bind_all("<F5>", self._shortcut_refresh)
        self.bind_all("<Control-Key-1>", lambda e: self._show("Tableau de bord"))
        self.bind_all("<Control-Key-2>", lambda e: self._show("Stock"))
        self.bind_all("<Control-Key-3>", lambda e: self._show("Réception"))
        self.bind_all("<Control-Key-4>", lambda e: self._show("Expédition"))
        self.bind_all("<Control-Key-5>", lambda e: self._show("Retour"))
        self.bind_all("<Control-Key-6>", lambda e: self._show("Inventaire"))
        self.bind_all("<Control-Key-7>", lambda e: self._show("Historique"))
        self.bind_all("<Control-Key-8>", lambda e: self._show("Produits"))
        self.bind_all("<Control-Key-9>", lambda e: self._show("Contacts"))
        self.bind_all("<Control-n>", self._shortcut_nouveau)
        self.bind_all("<Escape>", self._shortcut_escape)
        self.bind_all("<Control-Return>", self._shortcut_valider)

    def _rappels_fermeture(self) -> list[str]:
        """Points à rappeler avant de fermer. Aucun appel réseau ici.

        L'état des sauvegardes est celui déjà rapatrié à la connexion
        (`_verifier_sauvegarde`) : interroger le serveur au moment où
        l'opérateur clique sur la croix ferait geler la fenêtre, et il n'y a
        rien à gagner à connaître la sauvegarde à la seconde près.

        Un compte lecteur ou régional ne reçoit pas ces rappels : ils ne
        portent aucune responsabilité d'exploitation sur ce serveur.
        """
        try:
            en_attente = file_attente.nombre_en_attente()
        except Exception:
            en_attente = 0
        role = (self._user_info or {}).get("role")
        if role not in self.ROLES_CHECKLIST:
            # Un bon en attente reste un bon en attente, quel que soit le
            # rôle : cet avertissement-là a toujours existé et ne dépend pas
            # de la checklist d'exploitation.
            return lignes_checklist_fermeture(en_attente, None)
        return lignes_checklist_fermeture(
            en_attente, getattr(self, "_statut_sauvegarde", None))

    def _on_fermeture(self):
        rappels = self._rappels_fermeture()
        if rappels:
            from tkinter import messagebox
            corps = "\n\n".join(f"• {ligne}" for ligne in rappels)
            if not messagebox.askyesno(
                t("Avant de fermer"),
                f"{corps}\n\n" + t("Fermer quand même ?"),
            ):
                return
        self._fermer_websocket()
        self.destroy()

    def _session_expiree(self):
        """Redirige au login quand la session expire."""
        if hasattr(self, "_session_redirect_pending") and self._session_redirect_pending:
            return
        self._session_redirect_pending = True
        from tkinter import messagebox
        messagebox.showwarning(t("Session expirée"), t("Ta session a expiré. Reconnecte-toi."))
        self._terminer_session()
        self._session_redirect_pending = False

    def _shortcut_nouveau(self, event=None):
        """Ctrl+N : focus sur le formulaire actif ou naviguer vers Reception."""
        name = self._current_view_name()
        frame = self.view_instances.get(name) if name else None
        if frame and hasattr(frame, "product_combo"):
            frame.product_combo.focus_set()
        else:
            self._show("Réception")
            f = self.view_instances.get("Réception")
            if f and hasattr(f, "product_combo"):
                f.product_combo.focus_set()

    def _shortcut_escape(self, event=None):
        """Escape : vide le formulaire de la vue active (si c'est un DocumentFormView)."""
        name = self._current_view_name()
        frame = self.view_instances.get(name) if name else None
        if frame and hasattr(frame, "_clear_form"):
            frame._clear_form()

    def _shortcut_valider(self, event=None):
        """Ctrl+Entrée : valide le bon de la vue AFFICHÉE.

        Liaison unique et centralisée : Réception, Expédition et Retour sont
        trois DocumentFormView qui coexistent sans jamais être détruites. Un
        bind_all posé par chaque formulaire serait écrasé par le dernier
        construit, et Ctrl+Entrée aurait validé le bon du mauvais onglet.
        """
        name = self._current_view_name()
        frame = self.view_instances.get(name) if name else None
        if frame and hasattr(frame, "_submit"):
            frame._submit()

    def _shortcut_refresh(self, event=None):
        name = self._current_view_name()
        if not name:
            return
        frame = self.view_instances.get(name)
        if not frame:
            return
        if hasattr(frame, "refresh"):
            frame.refresh()
        elif hasattr(frame, "refresh_products"):
            frame.refresh_products()

    def _maj_badge_alertes(self, nb_critique: int, nb_alerte: int):
        badge = self._badge_labels.get("Stock")
        total = nb_critique + nb_alerte
        if total > 0 and badge:
            couleur = "#ef4444" if nb_critique else "#f59e0b"
            badge.configure(text=f" {total} ", fg_color=couleur)
            badge.pack(side="right", padx=(0, 8))
        elif badge:
            badge.pack_forget()

    def _maj_badge_alertes_regionales(self, nb: int):
        badge = self._badge_labels.get("Entrepôts régionaux")
        if not badge:
            return
        if nb > 0:
            badge.configure(text=f" {nb} ", fg_color="#ef4444")
            badge.pack(side="right", padx=(0, 8))
        else:
            badge.pack_forget()

    def _rafraichir_badge_alertes_regionales(self):
        """Recompte les ruptures signalées non résolues, toutes régions.

        Appelé au login admin, à chaque signalement reçu en direct (WebSocket)
        et après chaque résolution : le badge doit rester juste sans qu'un
        admin ait besoin d'ouvrir l'écran Alertes pour le savoir.
        """
        if not self._user_info or self._user_info.get("role") != "admin":
            return

        def _ok(data):
            self._maj_badge_alertes_regionales(data.get("nombre", 0))

        run_async(self, self.api.get_toutes_alertes_regionales, _ok, lambda _e: None)

    def _maj_badge_file(self):
        n = file_attente.nombre_en_attente()
        if n:
            self.badge_file.configure(
                text=t("  ⏳ {n} bon(s) en attente  ").format(n=n),
                text_color="white", fg_color="#92400e", corner_radius=6)
            # Le MÊME total est répété sur les trois onglets de saisie : la
            # file d'attente est unique et commune. Sans le préfixe « Total »,
            # trois badges « 2 » se lisaient comme six bons en attente.
            for nom_onglet in ("Réception", "Expédition", "Retour"):
                badge = self._badge_labels.get(nom_onglet)
                if badge:
                    badge.configure(text=" " + t("Total") + " %d " % n)
                    badge.pack(side="right", padx=(0, 8))
                    if not getattr(badge, "_infobulle_posee", False):
                        from views.dialogues import infobulle
                        # Texte calculé au survol : la file bouge en permanence.
                        infobulle(badge, lambda: t(
                            "Total global : {n} bon(s) en attente d'envoi, "
                            "tous types confondus (une seule file partagée "
                            "par les trois onglets).").format(
                                n=file_attente.nombre_en_attente()))
                        badge._infobulle_posee = True
        else:
            self.badge_file.configure(text="", fg_color="transparent")
            for nom_onglet in ("Réception", "Expédition", "Retour"):
                badge = self._badge_labels.get(nom_onglet)
                if badge:
                    badge.pack_forget()

    def _initialiser_websocket(self):
        """Initialise la connexion WebSocket pour les mises à jour en temps réel.

        Appelée uniquement après une connexion réussie : le serveur exige un
        token valide, sinon il ferme la connexion (code 4001).
        """
        if not self.config_data.get("server_host"):
            return
        if not getattr(self.api, "user_token", None):
            return

        # Fermer une éventuelle connexion précédente (changement d'utilisateur)
        self._fermer_websocket()
        self._indicateur_temps_reel.pack_forget()

        host = self.config_data["server_host"]
        port = self.config_data.get("server_port", 8000)

        self._websocket_client = WMSWebSocketClient(
            host=host,
            port=port,
            on_message_callback=self._websocket_message_received,
            token=self.api.user_token,
            ticket_provider=self.api.get_ws_ticket,
            on_giveup_callback=self._signaler_abandon_websocket,
            # Le superviseur de reconnexion peut retenter pendant ~55 minutes
            # (backoff jusqu'à 60 tentatives) avant l'abandon final — sans ces
            # deux callbacks, rien n'était montré à l'opérateur pendant toute
            # cette fenêtre, qui pouvait croire recevoir le temps réel d'autres
            # postes alors que ce n'était plus le cas. Repéré par audit de
            # robustesse le 9 septembre 2026.
            on_disconnect_callback=self._signaler_deconnexion_websocket,
            on_reconnect_callback=self._signaler_reconnexion_websocket,
            # Dérivée de l'adresse HTTP : c'est le seul calcul qui reste juste
            # quand le poste est configuré avec une URL complète (proxy HTTPS
            # d'un site distant) plutôt qu'avec une adresse IP.
            ws_url=websocket_url(self.config_data),
        )
        # Exposé sur l'api client : les vues (Stock, Tableau de bord) n'ont
        # accès qu'à self.api, pas à la fenêtre principale. Leur permet
        # d'espacer leur polling de secours tant que le WebSocket est vivant.
        self.api.websocket_client = self._websocket_client
        self._websocket_client.connect()

    def _signaler_abandon_websocket(self):
        """Appelé (thread de fond) quand la reconnexion WebSocket est abandonnée."""
        self._afficher_indicateur_temps_reel(
            t("⚠  Temps réel interrompu — cliquer pour relancer"))

    def _signaler_deconnexion_websocket(self):
        """Appelé (thread de fond) dès la PREMIÈRE coupure d'un épisode —
        avant l'abandon final, pendant que le superviseur retente en arrière-
        plan (jusqu'à ~55 minutes). Même indicateur que l'abandon, texte
        différent : cliquer relance quand même une tentative immédiate plutôt
        que d'attendre le prochain backoff."""
        self._afficher_indicateur_temps_reel(
            t("⚠  Temps réel interrompu — reconnexion en cours…"))

    def _signaler_reconnexion_websocket(self):
        """Appelé (thread de fond) quand la connexion revient après un
        épisode de coupure signalé — fait disparaître l'indicateur sans
        attendre un clic de l'opérateur."""
        try:
            self.after(0, self._indicateur_temps_reel.pack_forget)
        except Exception:
            pass

    def _afficher_indicateur_temps_reel(self, texte: str):
        try:
            self.after(0, lambda: (
                self._indicateur_temps_reel.configure(text=texte),
                self._indicateur_temps_reel.pack(
                    side="bottom", padx=12, pady=(0, 4)),
            ))
        except Exception:
            pass

    def _relancer_websocket(self):
        """Relance une connexion WebSocket depuis zéro (bouton de l'indicateur)."""
        self._indicateur_temps_reel.pack_forget()
        self._initialiser_websocket()

    def _fermer_websocket(self):
        """Ferme proprement la connexion WebSocket si elle existe."""
        if self._websocket_client is not None:
            try:
                self._websocket_client.disconnect()
            except Exception:
                pass
            self._websocket_client = None
            self.api.websocket_client = None

    def _websocket_message_received(self, message_type: str, data: dict):
        """Reçoit un message du thread WebSocket et le rejoue sur le thread Tk.

        Tkinter n'est pas thread-safe : toucher aux widgets depuis le thread
        WebSocket provoque des plantages aléatoires.
        """
        try:
            self.after(0, lambda: self._traiter_message_websocket(message_type, data))
        except Exception:
            pass

    def _traiter_message_websocket(self, message_type: str, data: dict):
        """Traite un message reçu du WebSocket (thread Tk)."""
        try:
            if message_type == "stock_update":
                self._handle_stock_update(data)
            elif message_type == "document_created":
                self._handle_document_created(data)
            elif message_type == "document_cancelled":
                self._handle_document_cancelled(data)
            elif message_type == "thresholds_updated":
                self._handle_thresholds_updated(data)
            elif message_type == "alert_triggered":
                self._handle_alert_triggered(data)
            elif message_type == "regional_alert_signaled":
                self._handle_regional_alert_signaled(data)
            # Ajouter d'autres types de messages selon les besoins
        except Exception as e:
            # Ne pas laisser les erreurs WebSocket crasher l'application.
            # (le module `journalisation` n'existe que côté serveur)
            import logging
            logging.getLogger(__name__).error(
                "Erreur lors du traitement du message WebSocket %s: %s",
                message_type, e)

    def _handle_stock_update(self, data: dict):
        """Met à jour l'affichage lorsqu'un stock change."""
        product_id = data.get("product_id")
        new_stock = data.get("new_stock")
        if product_id is not None and new_stock is not None:
            # Notifier les vues qui pourraient être intéressées
            # Par exemple, la vue Stock ou Tableau de bord
            vue_stock = self.view_instances.get("Stock")
            if vue_stock and hasattr(vue_stock, "maj_stock_produit"):
                vue_stock.maj_stock_produit(product_id, new_stock)

            vue_overview = self.view_instances.get("Tableau de bord")
            if vue_overview and hasattr(vue_overview, "maj_stock_produit"):
                vue_overview.maj_stock_produit(product_id, new_stock)

    def _handle_alert_triggered(self, data: dict):
        """Notifie un admin qu'un produit vient de passer sous son seuil.

        Diffusée depuis un autre poste (voir alert_triggered côté serveur,
        ciblé role_filter="admin") : sans ce toast, une rupture ne se
        découvrait qu'en ouvrant le tableau de bord.
        """
        if not self._user_info or self._user_info.get("role") != "admin":
            return
        nom = data.get("name") or data.get("sku") or t("un produit")
        stock = data.get("stock")
        seuil = data.get("seuil")
        texte = t("⚠ {nom} sous son seuil").format(nom=nom)
        if stock is not None and seuil is not None:
            texte += f" ({stock:g} / {seuil:g})"
        self._afficher_toast(texte)

    def _handle_regional_alert_signaled(self, data: dict):
        """Notifie un admin qu'une région vient de signaler une rupture.

        Même geste que _handle_alert_triggered, pour un signal manuel plutôt
        qu'un franchissement de seuil : sans ce toast, le central ne le
        découvrait qu'en ouvrant l'écran Alertes de la bonne région.
        """
        if not self._user_info or self._user_info.get("role") != "admin":
            return
        nom = data.get("name") or data.get("sku") or t("un produit")
        region = data.get("region") or t("une région")
        texte = t("⚠ {region} signale une rupture : {nom}").format(
            region=region, nom=nom)
        self._afficher_toast(texte)
        self._rafraichir_badge_alertes_regionales()

        vue = self.view_instances.get("Entrepôts régionaux")
        if vue is not None and hasattr(vue, "rafraichir_alertes"):
            vue.rafraichir_alertes()

    def _afficher_toast(self, texte: str, duree_ms: int = 6000):
        """Notification discrète et transitoire en haut du contenu principal."""
        toast = ctk.CTkLabel(
            self.content, text=texte, font=ctk.CTkFont(size=12, weight="bold"),
            text_color="white", fg_color="#b45309", corner_radius=8,
            padx=14, pady=8)
        toast.place(relx=0.5, rely=0.02, anchor="n")
        self.after(duree_ms, lambda: toast.destroy() if toast.winfo_exists() else None)

    def _afficher_bandeau_maj(self, version: str, url: str | None):
        """Bandeau persistant (pas un toast) : une mise à jour ne doit pas
        disparaître au bout de 6 secondes avant que quelqu'un l'ait vue.
        Jamais bloquant — l'opérateur continue de travailler en dessous."""
        bandeau = ctk.CTkFrame(self.content, fg_color="#1d4ed8", corner_radius=8)
        bandeau.place(relx=0.5, rely=0.02, anchor="n")
        ctk.CTkLabel(
            bandeau, text=t("⬆  Nouvelle version disponible : {v}").format(v=version),
            text_color="white", font=ctk.CTkFont(size=12, weight="bold")
        ).pack(side="left", padx=(14, 8), pady=8)
        if url:
            ctk.CTkButton(
                bandeau, text=t("Télécharger"), width=110, height=26,
                fg_color="white", text_color="#1d4ed8", hover_color="#e5e7eb",
                command=lambda: webbrowser.open(url)
            ).pack(side="left", padx=(0, 8), pady=8)
        ctk.CTkButton(
            bandeau, text="✕", width=28, height=26, fg_color="transparent",
            hover_color="#1e40af", text_color="white",
            command=bandeau.destroy
        ).pack(side="left", padx=(0, 10), pady=8)

    def _afficher_bandeau_sauvegarde(self, texte: str):
        """Bandeau persistant : la sauvegarde ne tourne plus.

        Même mécanisme que le bandeau de mise à jour (`_afficher_bandeau_maj`),
        en rouge et un cran plus bas pour ne pas le recouvrir quand les deux
        s'affichent. Jamais bloquant : l'entrepôt continue de travailler.
        """
        bandeau = ctk.CTkFrame(self.content, fg_color="#b91c1c", corner_radius=8)
        bandeau.place(relx=0.5, rely=0.10, anchor="n")
        ctk.CTkLabel(
            bandeau, text=texte, text_color="white",
            font=ctk.CTkFont(size=12, weight="bold")
        ).pack(side="left", padx=(14, 8), pady=8)
        ctk.CTkButton(
            bandeau, text="✕", width=28, height=26, fg_color="transparent",
            hover_color="#991b1b", text_color="white",
            command=bandeau.destroy
        ).pack(side="left", padx=(0, 10), pady=8)

    def _verifier_sauvegarde(self):
        """Contrôle unique après connexion d'un ADMIN : la sauvegarde est-elle
        encore fraîche ?

        Une seule vérification, pas de scrutation périodique : la sauvegarde
        tourne toutes les 6 heures, une alerte au moment où l'admin se
        connecte suffit largement, et rien ne doit ajouter du trafic de fond
        sur un réseau d'entrepôt.

        Totalement silencieux en cas d'échec (serveur plus ancien sans
        l'endpoint, réseau) : c'est une information, pas une erreur.
        """
        if not self._user_info or self._user_info.get("role") != "admin":
            return

        def _ok(data):
            # Mémorisé même quand tout va bien : la checklist de fermeture le
            # relit sans refaire d'appel réseau au moment où l'opérateur
            # clique sur la croix.
            self._statut_sauvegarde = data if isinstance(data, dict) else None
            if not data or not data.get("perimee"):
                return
            derniere = data.get("derniere_locale")
            seuil = data.get("seuil_heures", 48)
            if derniere:
                texte = t(
                    "⚠  Sauvegarde périmée : dernière sauvegarde le {date} "
                    "(plus de {h} h). Vérifiez que le serveur tourne."
                ).format(date=derniere[:16], h=seuil)
            else:
                texte = t(
                    "⚠  Aucune sauvegarde trouvée. Les données ne sont "
                    "actuellement protégées par aucune copie."
                )
            self._afficher_bandeau_sauvegarde(texte)

        run_async(self, self.api.get_backup_statut, _ok, lambda _e: None)

    # ------------------------------------------- checklists ouverture/fermeture

    # Rôles concernés par la checklist d'exploitation. Un lecteur ne saisit
    # rien et un compte régional n'a ni file d'attente centrale ni
    # responsabilité sur la sauvegarde du serveur : leur afficher ces rappels
    # ne ferait qu'ajouter du bruit qu'ils ne peuvent pas traiter.
    ROLES_CHECKLIST = ("admin", "magasinier")

    def _afficher_bandeau_checklist(self, lignes: list[str]):
        """Bandeau d'information de début de journée.

        Même mécanisme et même forme que `_afficher_bandeau_maj` et
        `_afficher_bandeau_sauvegarde` (cadre posé en `place()` au-dessus du
        contenu, croix de fermeture, jamais modal) : c'est volontaire, un
        troisième style de notification n'apprendrait rien de plus à
        l'opérateur. Placé encore un cran plus bas pour cohabiter avec les
        deux autres quand ils s'affichent le même jour.
        """
        if not lignes:
            return
        bandeau = ctk.CTkFrame(self.content, fg_color="#b45309", corner_radius=8)
        bandeau.place(relx=0.5, rely=0.18, anchor="n")
        textes = ctk.CTkFrame(bandeau, fg_color="transparent")
        textes.pack(side="left", padx=(14, 8), pady=8)
        ctk.CTkLabel(
            textes, text=t("📋  Point du matin"), text_color="white",
            font=ctk.CTkFont(size=12, weight="bold"), anchor="w",
        ).pack(anchor="w")
        for ligne in lignes:
            ctk.CTkLabel(
                textes, text=f"• {ligne}", text_color="white",
                font=ctk.CTkFont(size=11), anchor="w",
                wraplength=560, justify="left",
            ).pack(anchor="w")
        ctk.CTkButton(
            bandeau, text="✕", width=28, height=26, fg_color="transparent",
            hover_color="#92400e", text_color="white",
            command=bandeau.destroy
        ).pack(side="left", padx=(0, 10), pady=8)

    def _afficher_checklist_ouverture(self):
        """Rappels du début de session, uniquement s'il y a quelque chose à dire.

        Appelée depuis les rappels de `_verifier_sauvegarde` et
        `_verifier_reconciliation` : les deux arrivent de façon asynchrone et
        dans un ordre imprévisible. Un seul bandeau est affiché par session
        (`_checklist_affichee`), sinon deux réponses réseau produiraient deux
        bandeaux superposés.
        """
        if getattr(self, "_checklist_affichee", False):
            return
        role = (self._user_info or {}).get("role")
        if role not in self.ROLES_CHECKLIST:
            return
        try:
            en_attente = file_attente.nombre_en_attente()
        except Exception:
            en_attente = None
        lignes = lignes_checklist_ouverture(en_attente,
                                            getattr(self, "_nb_ecarts_stock", 0))
        if not lignes:
            return
        self._checklist_affichee = True
        self._afficher_bandeau_checklist(lignes)

    def _verifier_version(self):
        """Vérification silencieuse : jamais d'erreur affichée si ça échoue
        (ancien serveur sans l'endpoint, réseau coupé, etc.)."""
        def _ok(data):
            derniere = (data or {}).get("version")
            if derniere and derniere != client_version.VERSION:
                self._afficher_bandeau_maj(derniere, (data or {}).get("url"))

        run_async(self, self.api.get_version_disponible, _ok, lambda _e: None)

    def _handle_document_created(self, data: dict):
        """Traite la création d'un nouveau document."""
        # Rafraîchir l'historique si cette vue est affichée
        vue_historique = self.view_instances.get("Historique")
        if vue_historique and hasattr(vue_historique, "document_cree"):
            vue_historique.document_cree(data)

        # Notifier le tableau de bord qu'il pourrait avoir besoin de se mettre à jour
        vue_overview = self.view_instances.get("Tableau de bord")
        if vue_overview and hasattr(vue_overview, "notifier_nouveau_document"):
            vue_overview.notifier_nouveau_document(data)

    def _handle_document_cancelled(self, data: dict):
        """Traite l'annulation d'un document."""
        document_id = data.get("document_id")
        if document_id is not None:
            # Rafraîchir l'historique
            vue_historique = self.view_instances.get("Historique")
            if vue_historique and hasattr(vue_historique, "document_annule"):
                vue_historique.document_annule(data)
            # Les stocks concernés sont mis à jour par les messages
            # `stock_update` que le serveur envoie juste après l'annulation.

    def _handle_thresholds_updated(self, data: dict):
        """Un autre poste a modifié les seuils : recharge la vue Seuils."""
        vue_seuils = self.view_instances.get("Seuils")
        if vue_seuils and hasattr(vue_seuils, "seuils_mis_a_jour"):
            vue_seuils.seuils_mis_a_jour(data)

    REJEU_INTERVALLE_MS = 60000

    def _rejeu_automatique(self):
        """Tente toutes les minutes de renvoyer les bons restés hors ligne.

        Indépendant de la vue affichée. Silencieux en cas d'échec : le serveur
        est simplement encore injoignable, l'opérateur le voit déjà au badge.
        """
        try:
            if self._rejeu_en_cours:
                # Un rejeu qui n'a jamais rendu la main (session expirée
                # interceptée en amont) ne doit pas bloquer les suivants.
                self._rejeu_en_cours = False
            # Sans jeton, la requête recevrait un 401 pris pour une session
            # expirée : ce timer tourne dès le lancement, indépendamment du
            # login, et le déclenchait avant même que l'opérateur ait fini de
            # se connecter — ouvrant une seconde fenêtre de connexion en trop.
            elif self.api.user_token and file_attente.nombre_en_attente() > 0:
                self._rejeu_en_cours = True
                import async_call
                async_call.run_async(
                    self, lambda: file_attente.rejouer(self.api),
                    self._rejeu_termine, lambda _e: self._rejeu_termine(None))
        except Exception:
            self._rejeu_en_cours = False
        finally:
            self.after(self.REJEU_INTERVALLE_MS, self._rejeu_automatique)

    def _rejeu_termine(self, resultats):
        self._rejeu_en_cours = False
        self._maj_badge_file()
        if not resultats:
            return
        reussis = sum(1 for r in resultats if r.get("ok"))
        echecs = [r for r in resultats if not r.get("ok")]
        if reussis:
            self._notifier_discret(
                t("  ✓ {n} bon(s) en attente envoyé(s)  ").format(n=reussis),
                "#22c55e")
        if echecs:
            # Un bon refusé par le serveur (stock insuffisant, produit
            # inconnu) sort de la file : il faut le dire. Son contenu complet
            # est désormais conservé par `file_attente` dans
            # `file_attente_refuses.json` (`lire_refuses`), pour qu'il reste
            # ressaisissable après la fermeture de cette fenêtre.
            from tkinter import messagebox
            details = "\n".join(f"- {r.get('erreur', '?')}" for r in echecs)
            messagebox.showwarning(
                t("Bons refusés par le serveur"),
                t("{n} bon(s) en attente n'ont pas pu être enregistrés et ont "
                  "été retirés de la file :\n\n{details}\n\nLeur contenu est "
                  "conservé dans le fichier « {fichier} » du dossier de "
                  "configuration : ressaisis-les à partir de là si "
                  "nécessaire.").format(
                      n=len(echecs), details=details,
                      fichier="file_attente_refuses.json"))

    def _notifier_discret(self, texte: str, couleur: str):
        """Message temporaire dans la barre latérale, sans fenêtre bloquante."""
        self.badge_file.configure(text=texte, text_color=couleur)
        self.after(6000, self._maj_badge_file)

    def _afficher_details_file(self, _event=None):
        """Affiche les détails des bons en file d'attente au clic sur le badge."""
        n = file_attente.nombre_en_attente()
        if n == 0:
            return
        # Lecture directe de la file pour afficher les détails
        bons = file_attente._lire()
        details = []
        for i, bon in enumerate(bons[:20], 1):
            type_appel = bon.get("type_appel", "?")
            payload = bon.get("payload", {})
            label = {"document": t("Bon"),
                     "adjustment": t("Ajustement")}.get(type_appel, type_appel)
            party = payload.get("party") or t("sans tiers")
            doc_type = payload.get("doc_type") or payload.get("type") or ""
            type_label = {"RECEIVING": t("Réception"), "DELIVERY": t("Expédition"),
                          "RETURN": t("Retour")}.get(doc_type, doc_type)
            if type_label:
                details.append(f"  {i}. {label} ({type_label}) — {party}")
            else:
                details.append(f"  {i}. {label} — {party}")
        if n > 20:
            details.append(t("  ... et {n} autre(s)").format(n=n - 20))

        from tkinter import messagebox
        messagebox.showinfo(
            t("File d'attente"),
            t("{n} bon(s) en attente d'envoi au serveur :").format(n=n)
            + "\n\n"
            + "\n".join(details)
            + "\n\n"
            + t("Ils seront renvoyés automatiquement quand le serveur sera "
                "disponible."),
        )

    def _verifier_utilisateurs(self, tentative: int = 0):
        """Au démarrage, vérifie si des comptes existent et demande la connexion.

        LANCER.bat démarre le client quelques secondes après le serveur, sans
        garantie que celui-ci écoute déjà (sauvegarde de migration, machine
        lente...). Un seul essai laissait l'opérateur bloqué sans dialogue de
        connexion si le serveur n'était pas encore prêt : on retente pendant
        10 secondes avant d'abandonner.
        """
        try:
            status = self.api.users_status()
        except Exception:
            if tentative < 9:
                self.after(1000, lambda: self._verifier_utilisateurs(tentative + 1))
                return
            if not self.config_data["operator"]:
                self._open_settings()
            return

        if not status.get("has_users"):
            SetupAdminDialog(self, self.api, self._on_login)
        else:
            LoginDialog(self, self.api, self._on_login)

    def _on_login(self, result):
        self._user_info = result["user"]
        # Une reconnexion sous un autre compte doit pouvoir réafficher la
        # checklist du jour : le drapeau est remis à zéro ici, pas à
        # l'initialisation de la fenêtre.
        self._checklist_affichee = False
        self.config_data["operator"] = result["user"]["display_name"]
        save_config(self.config_data)
        # Le libellé sous « WMS Entrepôt » est construit une seule fois dans
        # _build_layout(), avant toute connexion, à partir du config.json
        # local (donc potentiellement l'opérateur d'une session précédente
        # sur ce poste). Sans cette ligne, il reste figé sur ce nom-là pour
        # toute la session, même après une connexion sous un autre compte.
        self._station_label.configure(
            text=self.config_data["operator"] or t("Poste local"))
        # Affiche le secteur du compte connecté à côté du titre de l'appli :
        # sur un poste qui sert Consumables ET FON, rien d'autre à l'écran
        # ne dit dans quel catalogue on se trouve.
        secteur = result["user"].get("sector")
        libelles_secteur = {"CONSUMABLES": "Consumables", "FON": "FON"}
        titre = t("WMS Entrepôt")
        if secteur:
            titre = f"{titre} / {libelles_secteur.get(secteur, secteur)}"
        self._titre_app_label.configure(text=titre)
        display = result["user"]["display_name"]
        # « Administrateur » ne dit pas que ce compte est le chef de
        # l'entrepôt CENTRAL du secteur (Idy509 pour Consumables, And509
        # pour FON) — distinction utile maintenant qu'un secteur peut avoir
        # ses propres entrepôts régionaux en dessous.
        role_labels = {"admin": t("WH Central"), "magasinier": t("Magasinier"),
                       "lecteur": t("Lecteur"), "regional": t("Entrepôt régional")}
        role = role_labels.get(result["user"]["role"], result["user"]["role"])
        if result["user"].get("region"):
            role = f"{role} — {result['user']['region']}"
        initials = "".join(p[0].upper() for p in display.split()[:2]) if display else "?"
        self._user_avatar.configure(text=initials)
        self.user_label.configure(text=display)
        self._user_role_label.configure(text=role)
        self._repack_footer()
        self._repack_nav(result["user"]["role"], result["user"].get("sector"))

        # Toutes les vues sont détruites et reconstruites à la connexion : elles
        # reliront `config_data["operator"]`, posé juste au-dessus. Propager
        # l'opérateur aux vues existantes ici (comme le fait `_open_settings`,
        # où elles survivent) ne servirait à rien — elles disparaissent la ligne
        # suivante.
        for name in list(self.view_instances):
            self.view_instances[name].destroy()
        self.view_instances.clear()
        # Un compte régional n'a pas accès au tableau de bord central : son
        # écran d'accueil est sa liste de réceptions à confirmer.
        self._show("Réception régionale" if result["user"]["role"] == "regional"
                   else "Tableau de bord")
        self._verifier_reconciliation()
        self._verifier_version()
        self._verifier_sauvegarde()
        self._rafraichir_badge_alertes_regionales()
        # Si le chargement initial (avant que le serveur soit prêt) avait
        # basculé sur le cache local, la connexion réussie est l'occasion de
        # se réaligner sur le référentiel serveur. `sector` connu à cet
        # instant (posé juste au-dessus) : sans lui, un poste servant
        # plusieurs secteurs pouvait retomber, en cas de coupure réseau juste
        # après la connexion, sur le référentiel en cache d'un AUTRE secteur.
        reference_data.charger_depuis_serveur(self.api, sector=secteur)
        # Le token est maintenant disponible : on peut ouvrir le WebSocket.
        self._initialiser_websocket()

    def _verifier_reconciliation(self):
        """Contrôle silencieux du grand livre à la connexion d'un administrateur.

        En arrière-plan et sans fenêtre : le tableau de bord affichera une
        ligne d'alerte discrète si — et seulement si — un écart existe.
        """
        self._nb_ecarts_stock = 0
        if not self._user_info or self._user_info.get("role") != "admin":
            # Un magasinier n'a pas droit au détail des écarts : sa checklist
            # ne porte que sur sa file d'attente locale, disponible tout de
            # suite, sans attendre de réponse réseau.
            self._afficher_checklist_ouverture()
            return

        def succes(data):
            self._nb_ecarts_stock = (data.get("nombre_ecarts", 0)
                                     + data.get("nombre_ecarts_emplacement", 0))
            vue = self.view_instances.get("Tableau de bord")
            if vue is not None:
                try:
                    vue._maj_ecarts()
                except Exception:
                    pass
            self._afficher_checklist_ouverture()

        # Un contrôle de cohérence ne doit jamais interrompre l'opérateur :
        # en cas d'échec réseau, on reste silencieux. La checklist s'affiche
        # quand même : elle porte aussi la file d'attente locale, qui est
        # justement l'information la plus utile quand le réseau ne va pas.
        import async_call

        async_call.run_async(self, self.api.get_stock_reconciliation,
                             succes, lambda e: self._afficher_checklist_ouverture())

    ONGLETS_RESERVES_ADMIN = ("Seuils", "Indicateurs", "Entrepôts régionaux",
                              "Qualité référentiel", "Utilisateurs",
                              "Journal d'audit", "État du système")
    ONGLETS_MASQUES_AU_LECTEUR = ("Réception", "Expédition", "Retour", "Inventaire")
    # La consigne bouteille n'existe que pour Consumables : un compte d'un
    # autre secteur (FON...) ne doit pas voir cet onglet, qui ne concerne
    # aucun de ses produits.
    ONGLETS_MASQUES_HORS_CONSUMABLES = ("Bouteilles",)
    # La reverse logistics (équipement prêté marqué RL_Apply, ex. routeurs
    # GPON FON) concerne FON et RAN — même principe que ci-dessus, dans
    # l'autre sens. RAN partage cette notion : du matériel réseau (RBS,
    # transmission) peut être prêté et doit revenir, comme demandé par
    # Idy509 le 12 septembre 2026.
    ONGLETS_MASQUES_HORS_FON_RAN = ("Retours équipement",)
    # Fuel ne connaît ni le retour de carburant (une livraison dispensée à un
    # véhicule n'est jamais rapportée à l'entrepôt), ni les entrepôts
    # régionaux : ce secteur n'a pas de régions du tout — il s'organise par
    # SITE (WH Central, Canapé-Vert), ce qui est une autre notion.
    #
    # « Entrepôts régionaux » restait visible pour l'admin Fuel (Rijkaard) :
    # un écran entier de Consumables/FON, vide de sens pour lui, dans le
    # secteur où il travaille. Signalé par Idy509 le 2026-09-08 — « les
    # secteurs ne doivent pas se mélanger ». À masquer pour FUEL SEULEMENT,
    # pas « hors Consumables » : FON a bel et bien des entrepôts régionaux
    # (Carrefour, Arcahaie, North, South).
    ONGLETS_MASQUES_POUR_FUEL = ("Retour", "Entrepôts régionaux")
    # Exactement l'inverse des deux listes ci-dessus : ces entrées n'existent
    # QUE pour un compte régional, qui de son côté ne voit rien d'autre.
    ONGLETS_REGIONAL = ("Réception régionale", "Transfert entre régions",
                        "Inventaire régional", "Alertes régionales",
                        "Historique régional")
    GROUPE_REGIONAL = "MA RÉGION"

    def _region_utilisateur(self) -> str | None:
        return (self._user_info or {}).get("region")

    def _regions_avec_entrepot(self) -> list[str]:
        """Régions dotées d'un entrepôt régional, seules destinations possibles.

        Port-au-Prince est desservi directement par le central : il n'y a pas
        d'entrepôt régional à y approvisionner, et le serveur refuserait un
        transfert vers cette région. Même exclusion que dans l'écran admin.
        """
        return [r for r in reference_data.regions() if r != "Port-au-Prince"]

    def _repack_nav(self, role: str | None, sector: str | None = None):
        """Réaffiche la navigation dans l'ordre canonique de NAV_ITEMS.

        `pack(after=...)` sans référence explicite retombe à la fin du
        parent : c'était le bug qui faisait réapparaître Réception/
        Expédition/Retour/Inventaire sous ADMINISTRATION après une
        reconnexion, au lieu de reprendre leur place sous MOUVEMENTS. En
        vidant tout puis en réempilant dans l'ordre de NAV_ITEMS, chaque
        entrée visible retrouve sa place par construction — aucune
        référence `after=` à tenir à jour.
        """
        for item_name, icon in self.NAV_ITEMS:
            if icon is None:
                self._group_labels[item_name].pack_forget()
            else:
                self.nav_buttons[item_name][0].pack_forget()

        est_regional = role == "regional"

        for item_name, icon in self.NAV_ITEMS:
            if icon is None:
                # Un compte régional ne voit que son propre groupe ; les autres
                # rôles ne voient jamais celui-là. Sans cela, un intitulé de
                # groupe resterait affiché au-dessus d'une section vide.
                if est_regional != (item_name == self.GROUPE_REGIONAL):
                    continue
                if item_name == "ADMINISTRATION" and role != "admin":
                    continue
                self._group_labels[item_name].pack(anchor="w", padx=10, pady=(12, 2))
                continue

            if est_regional != (item_name in self.ONGLETS_REGIONAL):
                continue
            if item_name in self.ONGLETS_RESERVES_ADMIN and role != "admin":
                continue
            if item_name in self.ONGLETS_MASQUES_AU_LECTEUR and role == "lecteur":
                continue
            if (item_name in self.ONGLETS_MASQUES_HORS_CONSUMABLES
                    and sector not in (None, "CONSUMABLES")):
                continue
            if (item_name in self.ONGLETS_MASQUES_HORS_FON_RAN
                    and sector not in ("FON", "RAN")):
                continue
            if item_name in self.ONGLETS_MASQUES_POUR_FUEL and sector == "FUEL":
                continue
            if item_name == "Inventaire":
                # Fuel ne fait pas d'inventaire produit : deux relevés de
                # cuve par jour (jaugeage), matin avant livraisons et soir
                # avant fermeture. Même écran, juste un intitulé qui parle
                # au métier.
                self.nav_buttons[item_name][1].configure(
                    text=t("Jaugeage") if sector == "FUEL" else t("Inventaire"))
            self.nav_buttons[item_name][0].pack(fill="x", pady=1)

    def _repack_footer(self):
        """Réempile le bas de la barre latérale dans un ordre fixe.

        Vider puis réempiler évite toute ambiguïté d'ordre entre les appels
        de connexion/déconnexion : la carte utilisateur ne peut jamais se
        retrouver chevauchée par le bouton Paramètres, quel que soit l'ordre
        dans lequel les widgets ont été créés ou précédemment affichés.
        """
        for widget in (self.user_frame, self.btn_parametres, self.btn_mdp, self.btn_deconnexion):
            widget.pack_forget()
        if self._user_info:
            self.user_frame.pack(fill="x", pady=(0, 6))
        self.btn_parametres.pack(fill="x", pady=(2, 2))
        if self._user_info:
            self.btn_mdp.pack(fill="x", pady=(2, 0))
            self.btn_deconnexion.pack(fill="x", pady=(2, 0))

    def _deconnecter(self):
        # Confirmation AVANT de couper la session : la déconnexion était
        # immédiate, et la fenêtre de connexion qui suit ferme toute
        # l'application si on l'annule (`LoginDialog._on_close`). Un clic
        # malheureux sur « Déconnexion » sortait donc de l'application sans
        # qu'aucune étape ne permette de revenir en arrière.
        #
        # Cette confirmation n'a de sens QUE pour une déconnexion
        # volontaire : pour une session déjà expirée côté serveur (voir
        # `_session_expiree`), il n'y a rien à « annuler » — le token est
        # déjà vide. Ne PAS appeler `_deconnecter` depuis ce cas-là.
        from tkinter import messagebox

        if not messagebox.askyesno(
                t("Déconnexion"),
                t("Se déconnecter de la session en cours ?")):
            return
        self._terminer_session()

    def _terminer_session(self):
        """Nettoie l'état de session et rouvre la fenêtre de connexion.

        Séparée de `_deconnecter` (qui demande confirmation) : appelée
        directement par `_session_expiree`, où la session est déjà morte
        côté serveur — redemander confirmation n'y a aucun sens, et un
        opérateur qui répondait « Non » par réflexe à cette confirmation
        laissait l'application dans un état incohérent (token déjà vidé
        par `api_client`, mais aucune fenêtre de connexion rouverte —
        repéré par audit de robustesse le 9 septembre 2026).
        """
        self._fermer_websocket()
        self.api.logout()
        self._user_info = None
        self.user_label.configure(text="")
        self._user_role_label.configure(text="")
        self._user_avatar.configure(text="")
        self._repack_footer()
        self.after(100, lambda: LoginDialog(self, self.api, self._on_login))

    def _changer_mon_mdp(self):
        ChangeMyPasswordDialog(self, self.api,
                               role=(self._user_info or {}).get("role", ""),
                               username=(self._user_info or {}).get("username", ""))

    def _open_settings(self):
        ancien_operateur = self.config_data.get("operator", "")

        def on_save(new_config):
            self.api.base_url = server_base_url(new_config)
            # La patience réseau suit l'adresse : un poste qu'on rebascule d'un
            # serveur local vers le serveur distant garderait sinon les délais
            # de l'ancienne configuration jusqu'au prochain redémarrage.
            self.api.timeout = timeouts(new_config)
            # Les formulaires déjà ouverts ne sont pas recréés. Ils reçoivent
            # donc le nouveau nom, sans écraser une saisie manuelle en cours.
            for vue in self.view_instances.values():
                actualiser = getattr(vue, "mettre_a_jour_operateur_defaut", None)
                if actualiser:
                    actualiser(ancien_operateur, new_config.get("operator", ""))

        SettingsDialog(self, self.config_data, on_save)


if __name__ == "__main__":
    app = App()
    app.mainloop()
