"""Entrepôts régionaux — Consommables : vue d'ensemble admin.

Un compte admin n'a pas de région propre — contrairement à un compte
"regional", qui ne voit jamais que la sienne. Ce wrapper ajoute un
sélecteur de région par-dessus les écrans déjà construits pour les comptes
régionaux (Réception / Transferts / Inventaire / Alertes), qui savent déjà
changer de région à la volée via `definir_region()`.

Un premier onglet « Vue d'ensemble » leur est posé devant : il montre TOUTES
les régions à la fois (stock déclaré, ruptures ouvertes, ancienneté du
comptage, note saisonnière), ce qu'aucun des écrans par région ne pouvait
donner — il fallait les ouvrir un à un pour reconstituer l'image d'ensemble.
"""
import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk

from async_call import run_async
from i18n import t
from reference_data import regions
from views.alertes_regionales import AlertesRegionalesView
from views.inventaire_regional import InventaireRegionalView
from views.reception_regionale import ReceptionRegionaleView
from views.transferts_regionaux import TransfertsRegionauxView


class EntrepotsRegionauxAdminView(ctk.CTkFrame):
    # « Vue d'ensemble » en premier : c'est l'écran qui répond à « où faut-il
    # envoyer du stock ? ». Les onglets suivants creusent UNE région à la fois,
    # sauf « Historique transferts », qui les montre toutes ensemble.
    ONGLETS = ("Vue d'ensemble", "Réception", "Transferts",
               "Historique transferts", "Inventaire", "Alertes")

    def __init__(self, master, api, on_alerte_change=None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self.on_alerte_change = on_alerte_change
        # Port-au-Prince reste l'entrepôt central, pas un entrepôt régional.
        self._regions = [r for r in regions() if r != "Port-au-Prince"]
        self._onglet_actif = "Vue d'ensemble"
        # Annotations libres par région, chargées une fois puis conservées :
        # region -> texte. Purement informatif (saisonnalité, accès dégradé).
        self._notes: dict[str, str] = {}
        self._build()
        self._charger_notes()
        self._charger_silence()

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 8))
        ctk.CTkLabel(top, text=t("🏬  Entrepôts régionaux — Consommables"),
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")

        self.region_var = tk.StringVar(value=self._regions[0] if self._regions else "")
        ctk.CTkComboBox(top, variable=self.region_var, values=self._regions,
                        width=200, state="readonly",
                        fg_color="#1a1d23", border_color="#2a2d35",
                        button_color="#2a2d35", button_hover_color="#353840",
                        dropdown_fg_color="#1a1d23",
                        command=lambda _: self._changer_region()).pack(side="right")
        ctk.CTkLabel(top, text=t("Région :"), text_color="#9ca3af").pack(
            side="right", padx=(0, 8))

        # --- Note de région : saisonnalité, accès dégradé ---
        # Un simple champ libre, sans aucune logique automatique derrière.
        # « Route difficile juin-septembre » ne déclenche rien : elle sert à
        # celui qui prépare un transfert et qui, sans ça, l'apprend par
        # téléphone une fois le camion parti.
        note_frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                                  border_width=1, border_color="#2a2d35")
        note_frame.pack(fill="x", padx=20, pady=(0, 8))
        ctk.CTkLabel(note_frame,
                     text=t("📝  Note de région (saisonnalité, accès) :"),
                     text_color="#9ca3af",
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(12, 8), pady=8)
        self.note_var = tk.StringVar()
        ctk.CTkEntry(note_frame, textvariable=self.note_var,
                     placeholder_text=t("Ex. : route difficile juin-septembre"),
                     height=30, corner_radius=8,
                     fg_color="#111318", border_color="#2a2d35").pack(
            side="left", fill="x", expand=True, pady=8)
        self._btn_note = ctk.CTkButton(
            note_frame, text=t("Enregistrer"), width=110, height=30,
            corner_radius=6, fg_color="#2563eb", hover_color="#1d4ed8",
            command=self._enregistrer_note)
        self._btn_note.pack(side="left", padx=12, pady=8)
        self._note_status = ctk.CTkLabel(note_frame, text="", text_color="#6b7280",
                                         font=ctk.CTkFont(size=10))
        self._note_status.pack(side="left", padx=(0, 12))

        # --- Silence régional ---
        # Une région qui ne saisit plus rien ressemble en tout point, sur les
        # autres écrans, à une région qui va bien : rien ne s'affiche dans les
        # deux cas. Cette bande dit la dernière fois que chaque région s'est
        # manifestée (comptage, bon, signalement de rupture).
        self._silence_frame = ctk.CTkFrame(self, fg_color="#1a1d23",
                                           corner_radius=10, border_width=1,
                                           border_color="#2a2d35")
        self._silence_frame.pack(fill="x", padx=20, pady=(0, 8))
        self._silence_titre = ctk.CTkLabel(
            self._silence_frame, text=t("📡  Activité des régions"),
            text_color="#9ca3af", font=ctk.CTkFont(size=11, weight="bold"))
        self._silence_titre.pack(anchor="w", padx=12, pady=(8, 2))
        self._silence_pastilles = ctk.CTkFrame(self._silence_frame,
                                               fg_color="transparent")
        self._silence_pastilles.pack(fill="x", padx=12, pady=(0, 8))

        onglets = ctk.CTkFrame(self, fg_color="transparent")
        onglets.pack(fill="x", padx=20, pady=(0, 8))
        self._boutons_onglet: dict[str, ctk.CTkButton] = {}
        for nom in self.ONGLETS:
            btn = ctk.CTkButton(
                onglets, text=t(nom), width=140, height=30, corner_radius=6,
                fg_color="#2563eb" if nom == self._onglet_actif else "#2a2d35",
                hover_color="#1d4ed8" if nom == self._onglet_actif else "#353840",
                command=lambda n=nom: self._changer_onglet(n))
            btn.pack(side="left", padx=(0, 8))
            self._boutons_onglet[nom] = btn

        self.conteneur = ctk.CTkFrame(self, fg_color="transparent")
        self.conteneur.pack(fill="both", expand=True)

        region0 = self.region_var.get()
        self._vues = {
            # Toutes les régions d'un coup : stock déclaré, ruptures ouvertes,
            # ancienneté du comptage et note saisonnière. Le sélecteur de
            # région au-dessus ne l'affecte pas — c'est justement la vue qui
            # évite d'avoir à passer les régions une par une.
            "Vue d'ensemble": TableauDeBordRegionsView(self.conteneur, self.api),
            # Lecture seule : l'admin ne confirme plus une réception à la
            # place de la région (le serveur le refuse désormais).
            "Réception": ReceptionRegionaleView(self.conteneur, self.api,
                                                region=region0, user_role="admin"),
            # Lecture seule : ce que la région a expédié vers une autre région
            # et qui n'est pas encore arrivé. L'admin ne transfère pas à sa
            # place, pour la même raison qu'il ne confirme pas à sa place.
            "Transferts": TransfertsRegionauxView(self.conteneur, self.api,
                                                  region=region0,
                                                  user_role="admin",
                                                  regions_possibles=self._regions),
            # Toutes régions confondues, contrairement à l'onglet Transferts
            # (une région à la fois, en attente seulement) : répond à « qui a
            # envoyé quoi à qui », complet et confirmé ou non.
            "Historique transferts": HistoriqueTransfertsRegionsView(
                self.conteneur, self.api),
            # Lecture seule : l'admin consulte les comptages envoyés par la
            # région, il ne compte pas son stock physique à sa place.
            "Inventaire": InventaireRegionalView(self.conteneur, self.api,
                                                 region=region0, user_role="admin"),
            # Cet écran n'est atteignable que par un admin : il doit y résoudre
            # les signalements, pas en créer.
            "Alertes": AlertesRegionalesView(self.conteneur, self.api,
                                             region=region0, user_role="admin",
                                             on_signal_change=self.on_alerte_change),
        }
        self._afficher_onglet(self._onglet_actif)

    def _changer_onglet(self, nom: str):
        self._onglet_actif = nom
        for n, btn in self._boutons_onglet.items():
            actif = n == nom
            btn.configure(fg_color="#2563eb" if actif else "#2a2d35",
                         hover_color="#1d4ed8" if actif else "#353840")
        self._afficher_onglet(nom)

    def _afficher_onglet(self, nom: str):
        for vue in self._vues.values():
            vue.pack_forget()
        vue = self._vues[nom]
        vue.pack(fill="both", expand=True)
        vue.refresh()

    def _changer_region(self):
        region = self.region_var.get()
        for vue in self._vues.values():
            vue.definir_region(region)
        self._afficher_note()
        self._vues[self._onglet_actif].refresh()

    # ------------------------------------------------- silence régional

    # Vert : la région s'est manifestée récemment.
    # Orange : plus rien depuis un moment, mais sous le seuil du serveur.
    # Rouge : silence caractérisé (seuil serveur atteint, ou aucune activité).
    SEUIL_ORANGE_JOURS = 3

    def _charger_silence(self):
        """Charge l'activité des régions. Un échec laisse l'écran utilisable."""
        def _ok(data):
            if not self._silence_pastilles.winfo_exists():
                return
            self._afficher_silence(data or {})

        run_async(self, self.api.get_regions_silence, _ok, lambda _e: None)

    def _afficher_silence(self, data: dict):
        for enfant in self._silence_pastilles.winfo_children():
            enfant.destroy()

        lignes = data.get("regions") or []
        if not lignes:
            ctk.CTkLabel(self._silence_pastilles,
                         text=t("Information indisponible"),
                         text_color="#6b7280",
                         font=ctk.CTkFont(size=11)).pack(side="left")
            return

        seuil = data.get("seuil_jours", 7)
        nb_silencieuses = data.get("nombre_silencieuses", 0)
        self._silence_titre.configure(
            text=t("📡  Activité des régions — {n} sans nouvelle depuis {j} jours "
                   "ou plus").format(n=nb_silencieuses, j=seuil)
            if nb_silencieuses else t("📡  Activité des régions — toutes actives"))

        for ligne in lignes:
            jours = ligne.get("jours_silence")
            if ligne.get("silencieuse"):
                couleur = "#7f1d1d"
                bord = "#ef4444"
            elif jours is not None and jours >= self.SEUIL_ORANGE_JOURS:
                couleur = "#78350f"
                bord = "#f59e0b"
            else:
                couleur = "#14532d"
                bord = "#4ade80"

            if jours is None:
                detail = t("aucune activité")
            elif jours == 0:
                detail = t("aujourd'hui")
            elif jours == 1:
                detail = t("hier")
            else:
                detail = t("il y a {j} j").format(j=jours)

            pastille = ctk.CTkFrame(self._silence_pastilles, fg_color=couleur,
                                    corner_radius=6, border_width=1,
                                    border_color=bord)
            pastille.pack(side="left", padx=(0, 6), pady=2)
            ctk.CTkLabel(pastille,
                         text=f"{ligne.get('region', '')} · {detail}",
                         text_color="#e5e7eb",
                         font=ctk.CTkFont(size=11)).pack(padx=8, pady=3)

    # ------------------------------------------------- notes de région

    def _charger_notes(self):
        """Charge les annotations. Un échec ne doit pas gêner l'écran."""
        def _ok(data):
            ancien = dict(self._notes)
            self._notes = dict(data.get("par_region") or {})
            # Ne pas écraser une note en cours de frappe : le rechargement a
            # lieu à chaque affichage de l'écran, et l'admin qui revient sur
            # l'onglet perdrait son texte non enregistré.
            region = self.region_var.get()
            if self.note_var.get() == ancien.get(region, ""):
                self._afficher_note()

        run_async(self, self.api.get_notes_regions, _ok, lambda _e: None)

    def _afficher_note(self):
        self.note_var.set(self._notes.get(self.region_var.get(), ""))
        self._note_status.configure(text="", text_color="#6b7280")

    def _enregistrer_note(self):
        region = self.region_var.get()
        if not region:
            return
        note = self.note_var.get().strip()
        self._btn_note.configure(state="disabled", text=t("Envoi…"))

        def _restaurer():
            if self._btn_note.winfo_exists():
                self._btn_note.configure(state="normal", text=t("Enregistrer"))

        def _ok(_res):
            _restaurer()
            # Note vide = annotation effacée : le cache local doit suivre,
            # sinon un changement de région la ferait réapparaître.
            if note:
                self._notes[region] = note
            else:
                self._notes.pop(region, None)
            self._note_status.configure(text=t("Enregistré"), text_color="#4ade80")

        def _err(exc):
            _restaurer()
            self._note_status.configure(text=str(exc), text_color="orange")

        run_async(self, lambda: self.api.set_note_region(region, note), _ok, _err)

    def refresh(self):
        self._charger_notes()
        self._charger_silence()
        if self._regions:
            self._vues[self._onglet_actif].refresh()

    def rafraichir_alertes(self):
        """Recharge l'écran Alertes même s'il n'est pas l'onglet affiché.

        Appelé par le toast temps réel : la liste doit être à jour dès que
        l'admin clique sur l'onglet, pas seulement après un rafraîchissement
        manuel.
        """
        self._vues["Alertes"].refresh()


class TableauDeBordRegionsView(ctk.CTkFrame):
    """Une ligne par région : les quatre informations, côte à côte.

    Jusqu'ici, savoir où en était une région demandait d'ouvrir quatre écrans
    (stock déclaré, alertes, date du dernier comptage, note saisonnière) et de
    recommencer pour chacune des douze régions. Ce tableau les met en regard,
    en un seul appel serveur.

    Lecture seule. La note est tronquée dans la colonne — elle sert de rappel,
    pas de texte à lire ici — et s'ouvre en entier au double-clic : une note
    d'accès (« route coupée juin-septembre ») fait souvent trois lignes.
    """

    # Vert / orange / rouge sur l'ancienneté du comptage : c'est la seule
    # colonne dont la valeur se dégrade toute seule avec le temps.
    SEUIL_ORANGE_JOURS = 14

    def __init__(self, master, api):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self._lignes: list[dict] = []
        self._build()

    def _build(self):
        entete = ctk.CTkFrame(self, fg_color="transparent")
        entete.pack(fill="x", padx=20, pady=(4, 4))
        self._resume = ctk.CTkLabel(
            entete, text="", text_color="#9ca3af",
            font=ctk.CTkFont(size=11), wraplength=900, justify="left")
        self._resume.pack(side="left")

        cadre = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                             border_width=1, border_color="#2a2d35")
        cadre.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        colonnes = ("region", "stock", "articles", "alertes", "transit",
                    "comptage", "note")
        self.tree = ttk.Treeview(cadre, columns=colonnes, show="headings")
        entetes = {"region": t("Région"), "stock": t("Stock déclaré"),
                   "articles": t("Articles comptés"),
                   "alertes": t("Ruptures ouvertes"),
                   "transit": t("En transit"),
                   "comptage": t("Dernier comptage"),
                   "note": t("Note de région")}
        largeurs = {"region": 140, "stock": 120, "articles": 130,
                    "alertes": 140, "transit": 100, "comptage": 190,
                    "note": 260}
        for col in colonnes:
            self.tree.heading(col, text=entetes[col])
            self.tree.column(col, width=largeurs[col], anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        barre = ttk.Scrollbar(cadre, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=barre.set)
        barre.pack(side="right", fill="y")
        self.tree.tag_configure("ok", foreground="#4ade80")
        self.tree.tag_configure("attention", foreground="#f59e0b")
        self.tree.tag_configure("grave", foreground="#ef4444")
        self.tree.bind("<Double-1>", lambda _e: self._ouvrir_note())

        self._status = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11),
                                    text_color="#6b7280")
        self._status.pack(anchor="w", padx=20, pady=(0, 12))

    # Le sélecteur de région de l'écran parent ne s'applique pas ici : ce
    # tableau les montre TOUTES. La méthode existe pour respecter le contrat
    # commun aux onglets (definir_region / refresh).
    def definir_region(self, region):
        return None

    def refresh(self):
        self._status.configure(text=t("Chargement…"), text_color="#6b7280")
        run_async(self, self.api.get_tableau_de_bord_regions,
                  self._afficher,
                  lambda exc: self._status.configure(text=str(exc),
                                                     text_color="orange"))

    def _afficher(self, data):
        data = data or {}
        self._lignes = data.get("regions") or []
        seuil = data.get("seuil_comptage_jours", 35)
        self.tree.delete(*self.tree.get_children())

        for ligne in self._lignes:
            jours = ligne.get("jours_depuis_comptage")
            if ligne.get("comptage_perime"):
                tag = "grave"
            elif jours is not None and jours >= self.SEUIL_ORANGE_JOURS:
                tag = "attention"
            else:
                tag = "ok"
            # Une rupture ouverte prime sur l'ancienneté du comptage : c'est
            # une demande explicite de la région, pas une déduction.
            if ligne.get("alertes_ouvertes"):
                tag = "grave"

            stock = ligne.get("stock_declare")
            note = (ligne.get("note") or "").replace("\n", " ")
            self.tree.insert("", "end", iid=ligne.get("region", ""),
                             tags=(tag,), values=(
                ligne.get("region", ""),
                "—" if stock is None else f"{stock:g}",
                ligne.get("articles_comptes") or 0,
                ligne.get("alertes_ouvertes") or 0,
                ligne.get("transferts_en_transit") or 0,
                self._libelle_comptage(ligne),
                (note[:60] + "…") if len(note) > 60 else (note or "—"),
            ))

        self._resume.configure(text=t(
            "{n} région(s) à entrepôt · {alertes} rupture(s) ouverte(s) · "
            "{perimes} comptage(s) de plus de {seuil} jours. "
            "Double-clic sur une ligne pour lire la note en entier.").format(
                n=data.get("nombre_regions", len(self._lignes)),
                alertes=data.get("total_alertes_ouvertes", 0),
                perimes=data.get("nombre_comptages_perimes", 0),
                seuil=seuil))
        self._status.configure(
            text=t("Stock déclaré = total du DERNIER comptage envoyé par la "
                   "région. Il est déclaratif : aucun stock n'en est déduit."),
            text_color="#6b7280")

    def _libelle_comptage(self, ligne: dict) -> str:
        dernier = (ligne.get("dernier_comptage") or "")[:16]
        jours = ligne.get("jours_depuis_comptage")
        if not dernier:
            return t("jamais")
        if jours is None:
            return dernier
        if jours == 0:
            return t("{date} (aujourd'hui)").format(date=dernier)
        return t("{date} (il y a {j} j)").format(date=dernier, j=jours)

    def _ouvrir_note(self):
        selection = self.tree.selection()
        if not selection:
            return
        region = selection[0]
        ligne = next((l for l in self._lignes if l.get("region") == region), None)
        if ligne is None:
            return
        note = (ligne.get("note") or "").strip()
        messagebox.showinfo(
            t("Note — {region}").format(region=region),
            note or t("Aucune note pour cette région."),
            parent=self)


class HistoriqueTransfertsRegionsView(ctk.CTkFrame):
    """Tous les transferts inter-régionaux, toutes régions confondues.

    Répond à « qui a envoyé quoi à qui » — aucun autre écran ne le montrait :
    le tableau de bord ne compte que les transferts EN TRANSIT par région
    destinataire, et l'onglet Transferts ne montre qu'une région à la fois,
    uniquement ce qui reste en attente de confirmation. Lecture seule, comme
    les autres onglets de ce compte : l'admin consulte, il n'expédie ni ne
    confirme à la place d'une région.
    """

    def __init__(self, master, api):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self._documents: list[dict] = []
        self._build()

    def _build(self):
        entete = ctk.CTkFrame(self, fg_color="transparent")
        entete.pack(fill="x", padx=20, pady=(4, 4))
        self._resume = ctk.CTkLabel(entete, text="", text_color="#9ca3af",
                                    font=ctk.CTkFont(size=11))
        self._resume.pack(side="left")

        cadre = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                             border_width=1, border_color="#2a2d35")
        cadre.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        colonnes = ("date", "expediteur", "destinataire", "statut",
                    "articles", "envoye_par")
        self.tree = ttk.Treeview(cadre, columns=colonnes, show="headings")
        entetes = {"date": t("Date"), "expediteur": t("Expéditeur"),
                   "destinataire": t("Destinataire"), "statut": t("Statut"),
                   "articles": t("Article(s)"), "envoye_par": t("Envoyé par")}
        largeurs = {"date": 130, "expediteur": 140, "destinataire": 140,
                    "statut": 110, "articles": 280, "envoye_par": 150}
        for col in colonnes:
            self.tree.heading(col, text=entetes[col])
            self.tree.column(col, width=largeurs[col], anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        barre = ttk.Scrollbar(cadre, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=barre.set)
        barre.pack(side="right", fill="y")
        self.tree.tag_configure("transit", foreground="#f59e0b")
        self.tree.tag_configure("annule", foreground="#6b7280")
        self.tree.bind("<Double-1>", lambda _e: self._ouvrir_detail())

        self._status = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11),
                                    text_color="#6b7280")
        self._status.pack(anchor="w", padx=20, pady=(0, 12))

    # Le sélecteur de région de l'écran parent ne s'applique pas ici : ce
    # tableau les montre TOUTES, comme la Vue d'ensemble.
    def definir_region(self, region):
        return None

    def refresh(self):
        self._status.configure(text=t("Chargement…"), text_color="#6b7280")
        run_async(
            self,
            lambda: self.api.list_documents(doc_type="REGIONAL_TRANSFER", limit=200),
            self._afficher,
            lambda exc: self._status.configure(text=str(exc), text_color="orange"),
        )

    def _afficher(self, resultat):
        documents, total = resultat
        self._documents = documents
        self.tree.delete(*self.tree.get_children())

        for doc in documents:
            annule = bool(doc.get("cancelled_at"))
            if annule:
                statut, tag = t("Annulé"), "annule"
            elif doc.get("received_at"):
                statut, tag = t("Reçu"), None
            else:
                statut, tag = t("En transit"), "transit"

            lignes = doc.get("lines") or []
            resume = ", ".join(
                f"{l.get('name', '')} ({l.get('quantity', 0):g})" for l in lignes[:3])
            if len(lignes) > 3:
                resume += t(" … +{n}").format(n=len(lignes) - 3)

            self.tree.insert(
                "", "end", iid=str(doc["id"]),
                tags=(tag,) if tag else (), values=(
                    (doc.get("created_at") or "")[:16],
                    doc.get("source_region") or "—",
                    doc.get("region") or "—",
                    statut,
                    resume or "—",
                    doc.get("created_by") or "—",
                ))

        self._resume.configure(text=t(
            "{n} transfert(s) affiché(s) sur {total} au total. "
            "Double-clic sur une ligne pour le détail complet.").format(
                n=len(documents), total=total))
        self._status.configure(text="", text_color="#6b7280")

    def _ouvrir_detail(self):
        selection = self.tree.selection()
        if not selection:
            return
        doc = next((d for d in self._documents if str(d["id"]) == selection[0]), None)
        if doc is None:
            return

        lignes = doc.get("lines") or []
        detail_lignes = "\n".join(
            f"• {l.get('name', '')} ({l.get('sku', '')}) : {l.get('quantity', 0):g}"
            for l in lignes) or t("Aucune ligne")

        if doc.get("cancelled_at"):
            statut = t("Annulé le {date}").format(date=(doc.get("cancelled_at") or "")[:16])
        elif doc.get("received_at"):
            statut = t("Reçu le {date} par {qui}").format(
                date=(doc.get("received_at") or "")[:16],
                qui=doc.get("received_by") or "—")
        else:
            statut = t("En transit")

        messagebox.showinfo(
            t("Transfert n°{id}").format(id=doc["id"]),
            t("{src} → {dst}\nEnvoyé le {date} par {qui}\nStatut : {statut}"
              "\n\n{lignes}").format(
                  src=doc.get("source_region") or "—",
                  dst=doc.get("region") or "—",
                  date=(doc.get("created_at") or "")[:16],
                  qui=doc.get("created_by") or "—",
                  statut=statut,
                  lignes=detail_lignes),
            parent=self)
