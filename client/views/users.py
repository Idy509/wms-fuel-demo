import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk

import reference_data
from async_call import run_async
from i18n import t
from views.utils import erreur_mot_de_passe

ROLE_REGIONAL = "regional"
CHOISIR_REGION = "— Région —"

ROLE_LIBELLES = {"admin": "WH Central", "magasinier": "Magasinier",
                 "lecteur": "Lecteur", ROLE_REGIONAL: "Entrepôt régional"}

# Ordre d'affichage du sélecteur de rôle à la création d'un compte.
ROLES_CREATION = ("magasinier", "lecteur", "admin", ROLE_REGIONAL)

# Secteurs possibles pour un nouveau compte (miroir de SECTEURS_VALIDES,
# server/models.py — à tenir à jour ensemble si RAN/Fuel/Spare s'ajoutent).
# Un admin ne voit ensuite QUE les comptes de son propre secteur (le serveur
# filtre la liste) : ce sélecteur ne sert qu'à la CRÉATION, notamment pour
# amorcer le tout premier compte d'un nouveau secteur.
SECTEURS_CREATION = ("CONSUMABLES", "FON", "RAN", "SPARES", "FUEL")


def regions_avec_entrepot() -> list[str]:
    """Les 12 régions dotées d'un entrepôt régional.

    Port-au-Prince en est exclue : elle est desservie directement par
    l'entrepôt central, il n'y a donc pas de compte régional à y créer.
    """
    return [r for r in reference_data.regions() if r != "Port-au-Prince"]


class UsersView(ctk.CTkFrame):
    """Vue admin pour gérer les comptes utilisateurs."""

    def __init__(self, master, api):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self._users: list[dict] = []
        # Formes traduites des libellés qui servent aussi de sentinelles ou
        # de clés : calculées ici, la langue n'étant fixée qu'après l'import.
        self._choisir_region = t(CHOISIR_REGION)
        self._roles_par_libelle = {t(ROLE_LIBELLES[c]): c for c in ROLES_CREATION}
        self._build()

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 8))
        ctk.CTkLabel(top, text=t("\U0001f465  Gestion des utilisateurs"),
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")
        ctk.CTkButton(top, text=t("↻  Rafraîchir"), width=110,
                      fg_color="#2a2d35", hover_color="#353840",
                      corner_radius=8, command=self.refresh).pack(side="right", padx=8)
        # Par défaut, un admin ne voit que les comptes de SON secteur. Cette
        # case permet à l'admin qui amorce les autres secteurs (aujourd'hui
        # Idy509, qui crée les premiers comptes FON/RAN/Fuel/Spare) de voir
        # tous les comptes existants, tous secteurs confondus.
        self.tous_secteurs_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(top, text=t("Tous les secteurs"),
                        variable=self.tous_secteurs_var,
                        command=self.refresh).pack(side="right", padx=8)

        form = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                            border_width=1, border_color="#2a2d35")
        form.pack(fill="x", padx=20, pady=8)
        ctk.CTkLabel(form, text=t("Créer un utilisateur"),
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="#f0f0f0").grid(
            row=0, column=0, columnspan=8, sticky="w", padx=8, pady=(10, 4))

        self.username_var = tk.StringVar()
        self.display_var = tk.StringVar()
        self.pass_var = tk.StringVar()
        self.role_var = tk.StringVar(value=t(ROLE_LIBELLES["magasinier"]))
        self.region_var = tk.StringVar(value=self._choisir_region)
        self.sector_var = tk.StringVar(value=SECTEURS_CREATION[0])
        self.site_var = tk.StringVar()

        ctk.CTkEntry(form, textvariable=self.username_var,
                     placeholder_text=t("Identifiant"), width=130).grid(
            row=1, column=0, padx=6, pady=8)
        ctk.CTkEntry(form, textvariable=self.display_var,
                     placeholder_text=t("Nom complet"), width=180).grid(
            row=1, column=1, padx=6, pady=8)
        ctk.CTkEntry(form, textvariable=self.pass_var,
                     placeholder_text=t("Mot de passe"), width=130, show="*").grid(
            row=1, column=2, padx=6, pady=8)
        ctk.CTkComboBox(form, variable=self.role_var,
                        values=list(self._roles_par_libelle),
                        width=130, state="readonly",
                        command=lambda _v: self._maj_champ_region()).grid(
            row=1, column=3, padx=6, pady=8)
        profil_frame = ctk.CTkFrame(form, fg_color="transparent")
        profil_frame.grid(row=1, column=4, padx=6, pady=8)
        ctk.CTkLabel(profil_frame, text=t("Profil"),
                     font=ctk.CTkFont(size=10), text_color="#6b7280").pack(anchor="w")
        ctk.CTkComboBox(profil_frame, variable=self.sector_var,
                        values=list(SECTEURS_CREATION),
                        width=130, state="readonly",
                        command=lambda _v: self._maj_champ_region()).pack()
        # Affichée seulement pour un compte régional : proposer une région à un
        # magasinier laisserait croire qu'elle change quelque chose.
        self.region_combo = ctk.CTkComboBox(
            form, variable=self.region_var,
            values=[self._choisir_region] + regions_avec_entrepot(),
            width=150, state="readonly")
        # Site physique (secteur FUEL uniquement, ex. "WH Central",
        # "Canapé-Vert") : filtre le catalogue visible pour un compte
        # non-admin. Texte libre — les sites n'ont pas de liste fermée comme
        # les régions, ils se créent au fur et à mesure des cuves.
        self.site_frame = ctk.CTkFrame(form, fg_color="transparent")
        ctk.CTkLabel(self.site_frame, text=t("Site (facultatif)"),
                     font=ctk.CTkFont(size=10), text_color="#6b7280").pack(anchor="w")
        ctk.CTkEntry(self.site_frame, textvariable=self.site_var, width=150).pack()
        self._btn_creer = ctk.CTkButton(form, text=t("Créer"), width=80,
                                        command=self._create)
        self._btn_creer.grid(row=1, column=6, padx=6, pady=8)

        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(fill="x", padx=20, pady=(0, 4))
        ctk.CTkLabel(info,
                     text=t("Magasinier = réception/expédition/inventaire  |  "
                            "Lecteur = consultation seulement  |  "
                            "Admin = tout  |  "
                            "Entrepôt régional = réception, inventaire et "
                            "alertes de SA région uniquement"),
                     text_color="#6b7280", font=ctk.CTkFont(size=10)).pack(anchor="w")

        table_frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                                   border_width=1, border_color="#2a2d35")
        table_frame.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        columns = ("username", "display_name", "role", "profile", "region", "site", "active", "created_at")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {"username": t("Identifiant"), "display_name": t("Nom"),
                    "role": t("Rôle"), "profile": t("Profil"),
                    "region": t("Région"), "site": t("Site"),
                    "active": t("Actif"), "created_at": t("Créé le")}
        widths = {"username": 140, "display_name": 180, "role": 130,
                  "profile": 110, "region": 120, "site": 120, "active": 70, "created_at": 150}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w")

        self.tree.tag_configure("inactive", foreground="#6b7280")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=20, pady=(0, 16))
        self._status = ctk.CTkLabel(bottom, text="", font=ctk.CTkFont(size=11))
        self._status.pack(side="left")

        btn_frame = ctk.CTkFrame(bottom, fg_color="transparent")
        btn_frame.pack(side="right")
        ctk.CTkButton(btn_frame, text=t("Désactiver"), width=110,
                      fg_color="#7a2a2a", hover_color="#943535",
                      command=self._toggle_active).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text=t("Changer le rôle"), width=120,
                      fg_color="#2a2d35", hover_color="#353840",
                      command=self._change_role).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text=t("Réinitialiser mot de passe"), width=180,
                      fg_color="#2a2d35", hover_color="#353840",
                      command=self._reset_password).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text=t("🌍 Régions du lecteur"), width=170,
                      fg_color="#2a2d35", hover_color="#353840",
                      command=self._modifier_regions_lecteur).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text=t("🏭 Site"), width=90,
                      fg_color="#2a2d35", hover_color="#353840",
                      command=self._modifier_site).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text=t("📋 Connexions"), width=130,
                      fg_color="#2a2d35", hover_color="#353840",
                      command=self._show_login_history).pack(side="left", padx=4)
        ctk.CTkButton(btn_frame, text=t("🔎 Revue des comptes"), width=170,
                      fg_color="#2a2d35", hover_color="#353840",
                      command=self._revue_des_comptes).pack(side="left", padx=4)


    def _maj_champ_region(self):
        if self._role_choisi() == ROLE_REGIONAL:
            self.region_combo.grid(row=1, column=5, padx=6, pady=8)
            self.site_frame.grid_remove()
            self._charger_regions_pour_secteur()
        else:
            self.region_combo.grid_remove()
            self.region_var.set(self._choisir_region)
            # Le site (secteur FUEL) partage la même colonne que la région :
            # les deux ne sont jamais pertinents en même temps (Fuel n'a pas
            # de compte régional aujourd'hui).
            if self.sector_var.get() == "FUEL":
                self.site_frame.grid(row=1, column=5, padx=6, pady=8)
            else:
                self.site_frame.grid_remove()
                self.site_var.set("")

    def _charger_regions_pour_secteur(self):
        """Repeuple le combo Région avec les régions DU SECTEUR choisi
        ("Profil"), pas forcément celui de l'admin connecté.

        Sans ça, un admin Consumables créant un compte régional FON verrait
        les régions Consumables (Cap-Haïtien, Jacmel...) dans la liste au
        lieu des régions FON (Carrefour, Arcahaie, North, South).
        """
        secteur = self.sector_var.get()

        def _ok(data):
            if not self.winfo_exists():
                return
            regions = [r for r in data.get("regions", []) if r != "Port-au-Prince"]
            self.region_combo.configure(values=[self._choisir_region] + regions)
            if self.region_var.get() not in regions:
                self.region_var.set(self._choisir_region)

        run_async(self, lambda: self.api.get_reference_regions(sector=secteur),
                  _ok, lambda _e: None)

    def _role_choisi(self) -> str:
        """Code de rôle correspondant au libellé affiché dans le sélecteur."""
        return self._roles_par_libelle.get(self.role_var.get(),
                                           self.role_var.get())

    def _erreur(self, exc):
        """Affiche une erreur d'appel serveur sans figer la fenêtre."""
        self._status.configure(text=str(exc), text_color="orange")

    def refresh(self):
        # Tous les appels serveur de cet écran passent par run_async : exécutés
        # sur la boucle Tk, ils gelaient toute l'application le temps du
        # timeout réseau (jusqu'à 30 s), sans même un bouton pour annuler.
        self._status.configure(text=t("Chargement…"), text_color="#6b7280")

        def _ok(users):
            self._users = users
            self._render()

        secteur = "ALL" if self.tous_secteurs_var.get() else None
        run_async(self, lambda: self.api.get_users(sector=secteur), _ok, self._erreur)

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        for u in self._users:
            tags = () if u["active"] else ("inactive",)
            self.tree.insert("", "end", iid=str(u["id"]), tags=tags, values=(
                u["username"],
                u["display_name"],
                t(ROLE_LIBELLES.get(u["role"], u["role"])),
                u.get("sector") or "CONSUMABLES",
                u.get("region") or "—",
                u.get("site") or "—",
                t("Oui") if u["active"] else t("Non"),
                u["created_at"][:16],
            ))
        self._status.configure(
            text=t("{n} utilisateur(s)").format(n=len(self._users)),
            text_color="#6b7280")

    def _selected_user(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            self._status.configure(text=t("Sélectionne un utilisateur"),
                                   text_color="orange")
            return None
        uid = int(sel[0])
        for u in self._users:
            if u["id"] == uid:
                return u
        return None

    def _create(self):
        username = self.username_var.get().strip()
        display = self.display_var.get().strip()
        password = self.pass_var.get()
        role = self._role_choisi()
        region = self.region_var.get()
        if role != ROLE_REGIONAL or region == self._choisir_region:
            region = ""
        sector = self.sector_var.get() or SECTEURS_CREATION[0]
        site = self.site_var.get().strip() if sector == "FUEL" else ""

        if not username or not display:
            self._status.configure(
                text=t("Tous les champs requis (mot de passe 10+ caractères)"),
                text_color="orange")
            return
        erreur = erreur_mot_de_passe(password, role, username=username)
        if erreur:
            self._status.configure(text=t(erreur), text_color="orange")
            return
        if role == ROLE_REGIONAL and not region:
            self._status.configure(
                text=t("Choisis la région de ce compte régional"),
                text_color="orange")
            return

        self._status.configure(text=t("Création en cours…"), text_color="#6b7280")
        # Le bouton reste verrouillé le temps de l'appel : sans cela, un
        # double clic partait deux fois et le second envoi échouait sur un
        # « identifiant déjà pris » incompréhensible pour l'administrateur.
        self._btn_creer.configure(state="disabled")

        def _restaurer():
            if self.winfo_exists():
                self._btn_creer.configure(state="normal")

        def _ok(_res):
            _restaurer()
            self.username_var.set("")
            self.display_var.set("")
            self.pass_var.set("")
            self.role_var.set(t(ROLE_LIBELLES["magasinier"]))
            self.sector_var.set(SECTEURS_CREATION[0])
            self.site_var.set("")
            self._maj_champ_region()
            self._status.configure(
                text=t("Utilisateur {nom} créé").format(nom=username),
                text_color="#22c55e")
            self.refresh()

        def _echec(exc):
            _restaurer()
            self._erreur(exc)

        run_async(self,
                  lambda: self.api.create_user(username, password, display,
                                                role=role, region=region or None,
                                                sector=sector, site=site or None),
                  _ok, _echec)

    def _toggle_active(self):
        user = self._selected_user()
        if not user:
            return
        new_state = not user["active"]
        question = (t("Réactiver {nom} ?") if new_state
                    else t("Désactiver {nom} ?"))
        if not messagebox.askyesno(
                t("Confirmation"),
                question.format(nom=user["display_name"])):
            return
        self._status.configure(text=t("Enregistrement…"), text_color="#6b7280")
        run_async(self,
                  lambda: self.api.update_user(user["id"], active=new_state),
                  lambda _res: self.refresh(), self._erreur)

    def _change_role(self):
        user = self._selected_user()
        if not user:
            return
        RoleDialog(self, self.api, user, on_saved=self.refresh)

    def _reset_password(self):
        user = self._selected_user()
        if not user:
            return
        PasswordDialog(self, self.api, user)

    def _modifier_site(self):
        """Rattache le compte sélectionné à un site physique (secteur FUEL).

        Filtre le catalogue produits que ce compte voit et peut utiliser :
        un compte non-admin ne voit que les produits de SON site (ou sans
        site défini) — voir `server/app.py::_site_du_compte`. Un admin
        (ex. Rijkaard) garde la vue globale quel que soit son propre site.
        """
        user = self._selected_user()
        if not user:
            return
        dialogue = ctk.CTkInputDialog(
            title=t("Site"),
            text=t("{nom} — site physique (ex. « WH Central », "
                   "« Canapé-Vert »).\n\nSite actuel : {actuel}\n\n"
                   "Vide = aucune restriction "
                   "(visible par tous les comptes du secteur).").format(
                       nom=user["display_name"],
                       actuel=user.get("site") or t("(aucun)")),
        )
        reponse = dialogue.get_input()
        if reponse is None:
            return
        site = reponse.strip()
        self._status.configure(text=t("Enregistrement…"), text_color="#6b7280")
        run_async(self,
                  lambda: self.api.update_user(user["id"], site=site or None),
                  lambda _res: self.refresh(), self._erreur)


    def _show_login_history(self):
        self._status.configure(text=t("Chargement de l'historique…"),
                               text_color="#6b7280")

        def _ok(history):
            self._status.configure(
                text=t("{n} utilisateur(s)").format(n=len(self._users)),
                text_color="#6b7280")
            LoginHistoryDialog(self, history)

        run_async(self, self.api.get_login_history, _ok, self._erreur)

    def _modifier_regions_lecteur(self):
        """Restreint un compte lecteur à une ou plusieurs régions."""
        user = self._selected_user()
        if not user:
            return
        if user["role"] != "lecteur":
            messagebox.showinfo(
                t("Comptes lecteurs uniquement"),
                t("Seul un compte « Lecteur » peut être restreint à des régions.\n\n"
                  "Un compte régional est déjà limité à sa région, un magasinier "
                  "travaille pour l'entrepôt central et un administrateur doit "
                  "tout voir."))
            return
        self._status.configure(text=t("Chargement…"), text_color="#6b7280")

        def _ok(data):
            self._status.configure(
                text=t("{n} utilisateur(s)").format(n=len(self._users)),
                text_color="#6b7280")
            RegionsLecteurDialog(self, self.api, user,
                                 data.get("regions") or [],
                                 on_saved=self.refresh)

        run_async(self, lambda: self.api.get_regions_utilisateur(user["id"]),
                  _ok, self._erreur)

    def _revue_des_comptes(self):
        """Liste les comptes et depuis quand chacun ne s'est plus connecté."""
        self._status.configure(text=t("Chargement de la revue…"),
                               text_color="#6b7280")

        def _ok(data):
            self._status.configure(
                text=t("{n} utilisateur(s)").format(n=len(self._users)),
                text_color="#6b7280")
            RevueComptesDialog(self, data.get("comptes") or [])

        run_async(self, self.api.get_revue_comptes, _ok, self._erreur)


class RegionsLecteurDialog(ctk.CTkToplevel):
    """Cases à cocher : les régions qu'un compte lecteur a le droit de voir.

    AUCUNE case cochée = aucune restriction, le lecteur voit toutes les
    régions. C'est l'état par défaut, et l'écran le dit explicitement : sans
    cela, « rien de coché » se lirait naturellement « il ne voit rien ».
    """

    def __init__(self, master, api, user: dict, regions_actuelles: list[str],
                 on_saved=None):
        super().__init__(master)
        self.api = api
        self.user = user
        self.on_saved = on_saved
        self.title(t("Régions — {nom}").format(nom=user["display_name"]))
        self.geometry("380x520")
        self.resizable(False, True)
        self.transient(master)
        self.lift()
        self.grab_set()

        ctk.CTkLabel(self, text=t("Régions visibles par ce lecteur"),
                     font=ctk.CTkFont(size=15, weight="bold")).pack(pady=(14, 4))
        ctk.CTkLabel(
            self,
            text=t("Aucune case cochée = ce lecteur voit TOUTES les régions."),
            text_color="#9ca3af", font=ctk.CTkFont(size=11),
            wraplength=330, justify="left").pack(padx=20, pady=(0, 8))

        liste = ctk.CTkScrollableFrame(self, fg_color="#1a1d23", corner_radius=10)
        liste.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        actuelles = set(regions_actuelles)
        self._cases: dict[str, tk.BooleanVar] = {}
        # Toutes les régions, Port-au-Prince comprise : le filtrage porte
        # aussi sur l'historique des bons, où PAP est bien présente.
        for region in reference_data.regions():
            var = tk.BooleanVar(value=region in actuelles)
            ctk.CTkCheckBox(liste, text=region, variable=var).pack(
                anchor="w", padx=12, pady=4)
            self._cases[region] = var

        self._status = ctk.CTkLabel(self, text="", text_color="orange",
                                    font=ctk.CTkFont(size=11))
        self._status.pack()

        barre = ctk.CTkFrame(self, fg_color="transparent")
        barre.pack(pady=(4, 12))
        ctk.CTkButton(barre, text=t("Annuler"), width=100, fg_color="gray30",
                      command=self.destroy).pack(side="left", padx=6)
        self._bouton = ctk.CTkButton(barre, text=t("Enregistrer"), width=140,
                                     fg_color="#2563eb", hover_color="#1d4ed8",
                                     command=self._save)
        self._bouton.pack(side="left", padx=6)

    def _save(self):
        choisies = [r for r, var in self._cases.items() if var.get()]
        self._bouton.configure(state="disabled", text=t("Enregistrement…"))

        def _ok(_res):
            if self.on_saved:
                self.on_saved()
            self.destroy()

        def _err(exc):
            if self.winfo_exists():
                self._bouton.configure(state="normal", text=t("Enregistrer"))
                self._status.configure(text=str(exc))

        run_async(self,
                  lambda: self.api.set_regions_utilisateur(self.user["id"], choisies),
                  _ok, _err)


class RevueComptesDialog(ctk.CTkToplevel):
    """Comptes et ancienneté de leur dernière connexion.

    Aucune action automatique : c'est une liste de travail. Un compte dormant
    depuis six mois n'est pas forcément à fermer, mais il doit au moins être
    passé sous les yeux de quelqu'un.
    """

    # Au-delà, la ligne passe en orange puis en rouge. Deux repères visuels
    # valent mieux qu'une colonne de chiffres qu'on ne lit plus.
    SEUIL_ATTENTION_JOURS = 90
    SEUIL_ALERTE_JOURS = 180

    def __init__(self, master, comptes: list[dict]):
        super().__init__(master)
        self.title(t("Revue des comptes"))
        self.geometry("880x480")
        self.resizable(True, True)
        self.transient(master)
        self.lift()
        self.grab_set()

        ctk.CTkLabel(self, text=t("Revue des comptes"),
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(12, 2))
        ctk.CTkLabel(
            self,
            text=t("Aucune désactivation automatique : cette liste sert à "
                   "décider, pas à agir."),
            text_color="#9ca3af", font=ctk.CTkFont(size=11)).pack(pady=(0, 8))

        frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                             border_width=1, border_color="#2a2d35")
        frame.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        cols = ("user", "role", "actif", "derniere", "inactif", "perimetre")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=16)
        entetes = {"user": t("Utilisateur"), "role": t("Rôle"),
                   "actif": t("Actif"), "derniere": t("Dernière connexion"),
                   "inactif": t("Inactif depuis"),
                   "perimetre": t("Régions autorisées")}
        largeurs = {"user": 190, "role": 130, "actif": 60, "derniere": 150,
                    "inactif": 110, "perimetre": 220}
        for col in cols:
            tree.heading(col, text=entetes[col])
            tree.column(col, width=largeurs[col], anchor="w")
        tree.tag_configure("attention", foreground="#f59e0b")
        tree.tag_configure("alerte", foreground="#ef4444")
        tree.tag_configure("inactive", foreground="#6b7280")
        tree.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        for compte in comptes:
            jours = compte.get("jours_inactif")
            if not compte.get("active"):
                tag = "inactive"
            elif jours is not None and jours >= self.SEUIL_ALERTE_JOURS:
                tag = "alerte"
            elif jours is not None and jours >= self.SEUIL_ATTENTION_JOURS:
                tag = "attention"
            else:
                tag = ""

            if compte.get("jamais_connecte"):
                derniere = t("Jamais")
                anciennete = (t("créé il y a {n} j").format(n=jours)
                              if jours is not None else "—")
            else:
                derniere = (compte.get("derniere_connexion") or "")[:16]
                anciennete = (t("{n} j").format(n=jours)
                              if jours is not None else "—")

            perimetre = compte.get("regions_autorisees") or []
            tree.insert("", "end", tags=(tag,) if tag else (), values=(
                f"{compte.get('display_name', '')} ({compte.get('username', '')})",
                t(ROLE_LIBELLES.get(compte.get("role"), compte.get("role") or "")),
                t("Oui") if compte.get("active") else t("Non"),
                derniere,
                anciennete,
                ", ".join(perimetre) if perimetre
                else (compte.get("region") or t("Toutes")),
            ))

        ctk.CTkButton(self, text=t("Fermer"), width=100, fg_color="gray30",
                      command=self.destroy).pack(pady=(0, 12))


class LoginHistoryDialog(ctk.CTkToplevel):
    def __init__(self, master, history: list[dict]):
        super().__init__(master)
        self.title(t("Historique de connexion"))
        self.geometry("550x400")
        self.resizable(True, True)
        self.transient(master)
        self.lift()
        self.grab_set()

        ctk.CTkLabel(self, text=t("Dernières connexions"),
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(12, 8))

        frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                             border_width=1, border_color="#2a2d35")
        frame.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        cols = ("date", "user", "status")
        tree = ttk.Treeview(frame, columns=cols, show="headings", height=15)
        tree.heading("date", text=t("Date"))
        tree.heading("user", text=t("Utilisateur"))
        tree.heading("status", text=t("Statut"))
        tree.column("date", width=160)
        tree.column("user", width=150)
        tree.column("status", width=100)
        tree.tag_configure("fail", foreground="#ef4444")
        tree.pack(side="left", fill="both", expand=True)

        sb = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")

        for h in history:
            ok = h.get("success", True)
            tag = () if ok else ("fail",)
            tree.insert("", "end", tags=tag, values=(
                h.get("created_at", "")[:19],
                h.get("username", ""),
                t("OK") if ok else t("Échec"),
            ))

        ctk.CTkButton(self, text=t("Fermer"), width=100, fg_color="gray30",
                      command=self.destroy).pack(pady=8)


class RoleDialog(ctk.CTkToplevel):
    def __init__(self, master, api, user, on_saved=None):
        super().__init__(master)
        self.api = api
        self.user = user
        self.on_saved = on_saved
        self._choisir_region = t(CHOISIR_REGION)
        self.title(t("Rôle — {nom}").format(nom=user["display_name"]))
        self.geometry("420x300")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        ctk.CTkLabel(self, text=t("Changer le rôle de {nom}").format(
                         nom=user["display_name"]),
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(16, 12))

        self.role_var = tk.StringVar(value=user["role"])
        for role, label in [
                ("magasinier", "Magasinier — réception, expédition, inventaire"),
                ("lecteur", "Lecteur — consultation seulement"),
                ("admin", "Administrateur — tout"),
                (ROLE_REGIONAL, "Entrepôt régional — sa région uniquement")]:
            ctk.CTkRadioButton(self, text=t(label), variable=self.role_var, value=role,
                               command=self._maj_champ_region).pack(
                anchor="w", padx=30, pady=2)

        self.region_var = tk.StringVar(
            value=user.get("region") or self._choisir_region)
        self.region_combo = ctk.CTkComboBox(
            self, variable=self.region_var,
            values=[self._choisir_region] + regions_avec_entrepot(),
            width=220, state="readonly")

        self._status = ctk.CTkLabel(self, text="", text_color="orange",
                                    font=ctk.CTkFont(size=11))
        self._status.pack(pady=(6, 0))

        self._bouton = ctk.CTkButton(self, text=t("Enregistrer"), width=120,
                                     fg_color="#2563eb", hover_color="#1d4ed8",
                                     command=self._save)
        self._bouton.pack(pady=12)
        self._maj_champ_region()

    def _maj_champ_region(self):
        if self.role_var.get() == ROLE_REGIONAL:
            self.region_combo.pack(pady=(8, 0), before=self._status)
        else:
            self.region_combo.pack_forget()

    def _save(self):
        role = self.role_var.get()
        region = self.region_var.get()
        if role == ROLE_REGIONAL and region == self._choisir_region:
            self._status.configure(text=t("Choisis la région de ce compte"))
            return

        # Le bouton est verrouillé pendant l'appel : sans cela, un double clic
        # part deux fois et la fenêtre reste figée le temps du réseau.
        self._bouton.configure(state="disabled", text=t("Enregistrement…"))

        def _ok(_res):
            if self.on_saved:
                self.on_saved()
            self.destroy()

        def _err(exc):
            if self.winfo_exists():
                self._bouton.configure(state="normal", text=t("Enregistrer"))
            messagebox.showerror(t("Erreur"), str(exc))

        # N'envoyer que ce qui change réellement. Envoyer `role` même
        # inchangé faisait passer le serveur par la branche « changement de
        # rôle », qui ne révoque pas la session : rattacher un compte régional
        # à une AUTRE région ne le déconnectait donc jamais, et son poste
        # continuait d'afficher les écrans de l'ancienne région jusqu'à la
        # prochaine reconnexion.
        champs = {}
        if role != self.user.get("role"):
            champs["role"] = role
        if role == ROLE_REGIONAL and (
                "role" in champs or region != (self.user.get("region") or "")):
            champs["region"] = region

        if not champs:
            self._bouton.configure(state="normal", text=t("Enregistrer"))
            self._status.configure(text=t("Aucune modification"))
            return

        run_async(self,
                  lambda: self.api.update_user(self.user["id"], **champs),
                  _ok, _err)


class PasswordDialog(ctk.CTkToplevel):
    def __init__(self, master, api, user):
        super().__init__(master)
        self.api = api
        self.user = user
        self.title(t("Mot de passe — {nom}").format(nom=user["display_name"]))
        self.geometry("320x180")
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()

        ctk.CTkLabel(self, text=t("Nouveau mot de passe pour {nom}").format(
                         nom=user["display_name"]),
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(16, 12))

        self.pass_var = tk.StringVar()
        ctk.CTkEntry(self, textvariable=self.pass_var,
                     placeholder_text=t("Nouveau mot de passe (10+ car.)"),
                     width=240, show="*").pack(pady=4)

        self.status = ctk.CTkLabel(self, text="", text_color="orange")
        self.status.pack()

        self._bouton = ctk.CTkButton(self, text=t("Changer"), width=120,
                                     fg_color="#2563eb", hover_color="#1d4ed8",
                                     command=self._save)
        self._bouton.pack(pady=8)

    def _save(self):
        pw = self.pass_var.get()
        # Le rôle du compte visé décide de la politique : réinitialiser le mot
        # de passe d'un admin exige la règle renforcée.
        erreur = erreur_mot_de_passe(pw, self.user.get("role", ""),
                                     username=self.user.get("username"))
        if erreur:
            self.status.configure(text=t(erreur))
            return
        self._bouton.configure(state="disabled", text=t("Envoi…"))

        def _ok(_res):
            messagebox.showinfo(
                t("OK"),
                t("Mot de passe changé pour {nom}").format(
                    nom=self.user["display_name"]))
            self.destroy()

        def _err(exc):
            if self.winfo_exists():
                self._bouton.configure(state="normal", text=t("Changer"))
                self.status.configure(text=str(exc))

        run_async(self,
                  lambda: self.api.update_user(self.user["id"], password=pw),
                  _ok, _err)
