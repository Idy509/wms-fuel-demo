import tkinter as tk
from datetime import date, datetime
from tkinter import filedialog, messagebox, ttk

from fpdf import FPDF
from tkcalendar import DateEntry

import customtkinter as ctk
from async_call import run_async
from i18n import t
from views.pdf_utils import setup_pdf_fonts, safe_text
from views.photo_utils import PHOTOS_DISPONIBLES, choisir_image, miniature
from views.print_utils import est_secteur_fuel, generer_pdf_bon
from views.utils import sort_treeview as _sort_treeview

from api_client import ApiError

MONTHS_FR = ["Janvier", "Février", "Mars", "Avril", "Mai", "Juin",
             "Juillet", "Août", "Septembre", "Octobre", "Novembre", "Décembre"]


def _parse_date(created_at: str) -> datetime | None:
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(created_at, fmt)
        except ValueError:
            continue
    return None


def _est_transfert_regional(doc: dict) -> bool:
    """Vrai si le bon est un transfert vers un entrepôt régional.

    Deux familles de bons passent par une confirmation de réception :

    - `REGIONAL_TRANSFER`, l'envoi d'une région vers une autre. Il en est UN
      par définition — sans ce cas, un transfert inter-régional parti mais
      pas encore confirmé s'affichait « Validé » dans l'historique, au même
      titre qu'un bon soldé : rien ne distinguait plus, à l'écran, la
      marchandise encore dans le camion de celle arrivée à destination.
    - `DELIVERY` vers une région, reconnu à la présence des champs de
      réception plutôt qu'en rejouant la règle du serveur : `received_at`
      renseigné, ou une expédition régionale sans technicien nommé.
      Port-au-Prince n'a pas d'entrepôt régional.
    """
    if doc.get("type") == "REGIONAL_TRANSFER":
        return True
    if doc.get("type") != "DELIVERY":
        return False
    if doc.get("received_at"):
        return True
    if (doc.get("technician") or "").strip():
        return False
    region = doc.get("region") or ""
    return bool(region) and region != "Port-au-Prince"


def _statut_valide(doc: dict) -> str:
    """Statut d'un bon non annulé.

    « En transit » : parti de l'entrepôt central, pas encore confirmé reçu par
    la région. « Reçu » : la région a confirmé les quantités arrivées.
    """
    if not _est_transfert_regional(doc):
        return "Validé"
    return "Reçu" if doc.get("received_at") else "En transit"


class HistoryView(ctk.CTkFrame):
    PAGE = 200

    def __init__(self, master, api, user_role: str | None = None,
                 sector: str | None = None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        # Le secteur du compte connecté, transmis par main.py comme pour
        # DocumentFormView/InventoryView. Il décide des colonnes du tableau,
        # du filtre de type et du contenu du ticket PDF — pour tout autre
        # secteur que FUEL, rien ne change.
        self.sector = sector
        self._is_fuel = est_secteur_fuel(sector)
        # Un lecteur voit l'historique (c'est sa raison d'être) mais ne peut ni
        # annuler ni dupliquer un bon : le serveur le refuse (403). Afficher les
        # boutons pour les lui refuser ensuite n'apprend rien à un magasinier ;
        # « Dupliquer » l'emmenait même vers l'écran de saisie, pourtant retiré
        # de sa navigation.
        self._user_role = user_role
        self._lecture_seule = user_role == "lecteur"
        self._bons_par_ligne: dict[str, dict] = {}
        self._offset = 0
        self._total = 0
        self._products: list[dict] = []
        self._product_map: dict[str, int] = {}
        self._known_operators: list[str] = []
        self._build()

    def _build(self):
        # Le bouton d'annulation est volontairement placé dans sa propre barre
        # d'action. Il ne peut ainsi pas disparaître quand la largeur de la
        # fenêtre change ou quand des actions sont ajoutées à l'en-tête.
        ligne1 = ctk.CTkFrame(self, fg_color="transparent")
        ligne1.pack(fill="x", padx=20, pady=(20, 4))

        ctk.CTkLabel(ligne1, text=t("🕐  Historique des mouvements"),
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")

        ctk.CTkButton(ligne1, text=t("📥  Exporter Excel"), width=150,
                      fg_color="#1a1d23", hover_color="#2a2d35",
                      border_width=1, border_color="#2a2d35",
                      corner_radius=8, command=self._export).pack(side="right")
        ctk.CTkButton(ligne1, text=t("📄  Export PDF"), width=130,
                      fg_color="#1a1d23", hover_color="#2a2d35",
                      border_width=1, border_color="#2a2d35",
                      corner_radius=8, command=self._export_pdf).pack(side="right", padx=4)
        ctk.CTkButton(ligne1, text=t("↻  Rafraîchir"), width=110,
                      fg_color="#2a2d35", hover_color="#353840",
                      corner_radius=8, command=self.refresh).pack(side="right", padx=8)

        barre_action = ctk.CTkFrame(self, corner_radius=8, fg_color="#1a1d23",
                                    border_width=1, border_color="#2a2d35")
        barre_action.pack(fill="x", padx=20, pady=(0, 4))
        ctk.CTkLabel(barre_action,
                     text=t("Action sur le bon sélectionné :")).pack(
            side="left", padx=(12, 8), pady=8
        )
        self.bouton_dupliquer = ctk.CTkButton(
            barre_action, text=t("Dupliquer"), width=120,
            fg_color="#2a2d35", hover_color="#353840",
            command=self._dupliquer_bon, state="disabled",
        )
        if not self._lecture_seule:
            self.bouton_dupliquer.pack(side="left", pady=8, padx=(0, 4))
        self.bouton_imprimer = ctk.CTkButton(
            barre_action, text=t("Imprimer"), width=120,
            fg_color="#2a2d35", hover_color="#353840",
            command=self._imprimer_bon, state="disabled",
        )
        self.bouton_imprimer.pack(side="left", pady=8, padx=(0, 4))
        # Pièces jointes : colis abîmé, bordereau signé. Un lecteur peut les
        # consulter (le serveur sert les images à tout compte connecté) mais
        # pas en ajouter — le bouton reste, le dialogue masque l'ajout.
        self.bouton_photos = ctk.CTkButton(
            barre_action, text=t("📷  Photos"), width=120,
            fg_color="#2a2d35", hover_color="#353840",
            command=self._photos_bon, state="disabled",
        )
        self.bouton_photos.pack(side="left", pady=8, padx=(0, 4))
        self.bouton_annuler = ctk.CTkButton(
            barre_action, text=t("Annuler le bon"), width=170,
            fg_color="#8a2f2f", hover_color="#a33a3a",
            command=self._annuler_bon, state="disabled",
        )
        if not self._lecture_seule:
            self.bouton_annuler.pack(side="left", pady=8)
        self.aide_annulation = ctk.CTkLabel(
            barre_action,
            text=t("Sélectionne une ligne pour l'imprimer.") if self._lecture_seule
            else t("Sélectionne une ligne pour annuler son bon."),
            text_color="gray"
        )
        self.aide_annulation.pack(side="left", padx=12)

        ligne2 = ctk.CTkFrame(self, fg_color="transparent")
        ligne2.pack(fill="x", padx=20, pady=(0, 4))

        # Style sombre cohérent avec le reste de l'appli (Stock, Tableau de
        # bord) : sans ces couleurs, les combobox gardaient le thème bleu par
        # défaut de CTk et détonnaient sur fond sombre.
        combo_style = dict(fg_color="#1a1d23", border_color="#2a2d35",
                           button_color="#2a2d35", button_hover_color="#353840",
                           dropdown_fg_color="#1a1d23", corner_radius=8, height=30)

        # La combo affiche des libellés traduits mais _selected_filter()
        # raisonne sur les clés françaises : ce dictionnaire fait le pont.
        # En français, t(cle) == cle et la table est l'identité.
        # Pour le carburant, « Ajustements » et « Inventaires » désignent le
        # MÊME geste — le jaugeage de cuve (l'écran de saisie s'appelle déjà
        # « Jaugeage », voir client/views/inventory.py). Constaté à l'usage :
        # l'opérateur cherchait son jaugeage dans « Inventaires » et ne
        # trouvait rien, car il est enregistré en ADJUSTMENT sans préfixe de
        # référence. Une seule entrée « Jaugeages », sur le type ADJUSTMENT,
        # les ramène tous. Pas de « Retours » non plus : l'onglet Retour est
        # masqué pour ce secteur (on ne rend pas du carburant).
        if self._is_fuel:
            cles = ("Tous", "Réceptions", "Expéditions", "Jaugeages",
                    "Transferts régionaux")
        else:
            cles = ("Tous", "Réceptions", "Expéditions",
                    "Retours", "Retours fournisseur",
                    "Ajustements", "Inventaires",
                    "Transferts régionaux")
        self._cles_filtre = {t(cle): cle for cle in cles}
        self._tous = t("Tous")
        self._tous_produits = t("Tous les produits")
        self.filter_var = tk.StringVar(value=self._tous)
        ctk.CTkComboBox(ligne2, variable=self.filter_var, width=140,
                        values=list(self._cles_filtre),
                        command=lambda _: self.refresh(), **combo_style).pack(side="left")

        self.product_var = tk.StringVar(value=self._tous_produits)
        self.product_combo = ctk.CTkComboBox(
            ligne2, variable=self.product_var, width=200,
            values=[self._tous_produits], state="readonly",
            command=lambda _: self.refresh(), **combo_style)
        self.product_combo.pack(side="left", padx=(8, 0))

        self.operator_var = tk.StringVar(value=self._tous)
        self.operator_combo = ctk.CTkComboBox(
            ligne2, variable=self.operator_var, width=160,
            values=[self._tous], state="readonly",
            command=lambda _: self.refresh(), **combo_style)
        self.operator_combo.pack(side="left", padx=(8, 0))

        # Recherche filtrée côté serveur (sur tous les documents, pas
        # seulement la page chargée) : anti-rebond de 500 ms pour ne pas
        # envoyer une requête à chaque frappe.
        self._recherche_job = None
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._debounce_recherche())
        ctk.CTkEntry(ligne2, textvariable=self.search_var,
                     placeholder_text=t("🔍  SKU / produit / réf..."),
                     width=200, corner_radius=8, height=30,
                     fg_color="#1a1d23", border_color="#2a2d35").pack(side="left", padx=8)

        date_frame = ctk.CTkFrame(ligne2, fg_color="transparent")
        date_frame.pack(side="left", padx=(8, 0))

        self._configurer_style_dates()

        # Les 6 listes Année/Mois/Jour + le bouton "Filtrer" donnaient
        # l'impression que rien ne se passait. Deux vrais calendriers, et le
        # filtre s'applique dès qu'une date est choisie (comme le filtre Type).
        self.date_filter_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(date_frame, text=t("Filtrer par date"),
                        variable=self.date_filter_var,
                        font=ctk.CTkFont(size=11),
                        checkbox_width=18, checkbox_height=18,
                        fg_color="#2563eb", hover_color="#1d4ed8",
                        border_color="#2a2d35",
                        command=self._on_date_filter_toggle).pack(side="left", padx=(0, 8))

        today = date.today()
        calendrier_style = dict(
            style="Dark.DateEntry", width=11, state="readonly",
            date_pattern="dd/mm/yyyy", firstweekday="monday",
            showweeknumbers=False,
            year=today.year, month=today.month, day=today.day,
            background="#1a1d23", foreground="#e5e7eb",
            bordercolor="#2a2d35",
            headersbackground="#1a1d23", headersforeground="#9ca3af",
            normalbackground="#1a1d23", normalforeground="#e5e7eb",
            weekendbackground="#22252c", weekendforeground="#e5e7eb",
            othermonthbackground="#15171c", othermonthforeground="#4b5563",
            othermonthwebackground="#15171c", othermonthweforeground="#4b5563",
            selectbackground="#2563eb", selectforeground="#ffffff",
        )

        ctk.CTkLabel(date_frame, text=t("Du"), font=ctk.CTkFont(size=11)).pack(side="left")
        self.from_date = DateEntry(date_frame, **calendrier_style)
        self.from_date.pack(side="left", padx=4)
        self.from_date.bind("<<DateEntrySelected>>",
                            lambda _e: self._on_date_selected("from"))

        ctk.CTkLabel(date_frame, text=t("  Au"), font=ctk.CTkFont(size=11)).pack(side="left")
        self.to_date = DateEntry(date_frame, **calendrier_style)
        self.to_date.pack(side="left", padx=4)
        self.to_date.bind("<<DateEntrySelected>>",
                          lambda _e: self._on_date_selected("to"))

        ctk.CTkButton(date_frame, text="✕", width=30, height=28,
                      fg_color="#2a2d35", hover_color="#353840",
                      font=ctk.CTkFont(size=11),
                      command=self._clear_dates).pack(side="left", padx=(6, 0))

        ligne3 = ctk.CTkFrame(self, fg_color="transparent")
        ligne3.pack(fill="x", padx=20, pady=(0, 8))

        self.bouton_suivant = ctk.CTkButton(ligne3, text=t("Page suivante ›"), width=120,
                                             fg_color="gray30", command=self._page_suivante)
        self.bouton_suivant.pack(side="right")
        self.bouton_precedent = ctk.CTkButton(ligne3, text=t("‹ Page précédente"), width=130,
                                               fg_color="gray30", command=self._page_precedente)
        self.bouton_precedent.pack(side="right", padx=8)

        table_frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                                   border_width=1, border_color="#2a2d35")
        table_frame.pack(fill="both", expand=True, padx=20, pady=(0, 20))

        # Part Number juste après ITEM, comme dans l'export : plusieurs SKU
        # partagent souvent le même libellé (ex. 6 références
        # "Fuel Filter (P11/P16)"), sans lui une ligne n'est pas identifiable.
        if self._is_fuel:
            # Colonnes du métier carburant : Location/Region/Supervisor/Part
            # Number restaient systématiquement vides sur un compte FUEL,
            # pendant que la plaque, le projet ou le kilométrage — déjà
            # stockés sur `documents` (migration 51) — n'apparaissaient nulle
            # part. Libellés en français : ces colonnes ne viennent d'aucun
            # tracker Excel anglophone, contrairement à celles de Consumables.
            columns = ("statut", "type", "item", "date", "month", "qty",
                       "receipt", "receiver", "plate", "vehicle", "project",
                       "mileage", "fuel_card", "comments")
            headings = {
                "statut": t("Statut"), "type": t("Type"), "item": t("Cuve"),
                "date": t("Date"), "month": t("Mois"), "qty": t("Qté (gls)"),
                "receipt": t("N° Bon"), "receiver": t("Receveur"),
                "plate": t("Plaque"), "vehicle": t("Véhicule"),
                "project": t("Projet"), "mileage": t("Km"),
                "fuel_card": t("Carte carburant"), "comments": t("Commentaires"),
            }
            widths = {"statut": 80, "type": 90, "item": 200, "date": 110,
                      "month": 90, "qty": 80, "receipt": 90, "receiver": 150,
                      "plate": 110, "vehicle": 150, "project": 130,
                      "mileage": 80, "fuel_card": 120, "comments": 200}
        else:
            columns = ("statut", "type", "location", "item", "part_number", "date", "month", "qty",
                        "receipt", "receiver", "region", "supervisor", "comments")
            headings = {
                "statut": t("Statut"), "type": t("Type"), "location": t("Emplacement"),
                "item": t("Article"),
                "part_number": t("N° Pièce"), "date": t("Date livraison"), "month": t("Mois"),
                "qty": t("Qté"), "receipt": t("N° Reçu"), "receiver": t("Receveur"),
                "region": t("Région"), "supervisor": t("Superviseur"),
                "comments": t("Commentaires"),
            }
            widths = {"statut": 80, "type": 90, "location": 90, "item": 220, "part_number": 110,
                      "date": 110, "month": 90, "qty": 70, "receipt": 90, "receiver": 150,
                      "region": 130, "supervisor": 160, "comments": 200}
        # Mémorisé pour que plus aucun code ne lise le treeview par indice
        # numérique : les colonnes ne sont plus les mêmes d'un secteur à
        # l'autre (voir `_valeur_colonne`).
        self._colonnes = columns
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        for col in columns:
            self.tree.heading(col, text=headings[col],
                              command=lambda c=col: _sort_treeview(self.tree, c))
            self.tree.column(col, width=widths[col], anchor="w")

        self.tree.tag_configure("annule", background="#3a1a1a", foreground="#aa6666")
        # En transit : le stock a quitté l'entrepôt central mais n'est pas
        # encore confirmé arrivé. Le distinguer visuellement évite de le
        # confondre avec un mouvement terminé.
        self.tree.tag_configure("transit", foreground="#f59e0b")
        self.tree.bind("<<TreeviewSelect>>", self._mettre_a_jour_action_annulation)

        scrollbar_y = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        scrollbar_x = ttk.Scrollbar(table_frame, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=scrollbar_y.set, xscrollcommand=scrollbar_x.set)
        scrollbar_x.pack(side="bottom", fill="x")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar_y.pack(side="right", fill="y")

        self.status_label = ctk.CTkLabel(self, text="", text_color="orange")
        self.status_label.pack(anchor="w", padx=16, pady=(0, 8))

    def _selected_filter(self) -> tuple[str | None, str | None]:
        """Retourne (type, reference_prefix) selon le filtre choisi."""
        filtre = self._cles_filtre.get(self.filter_var.get(),
                                       self.filter_var.get())
        if filtre == "Inventaires":
            return "ADJUSTMENT", "Inventaire complet"
        return {"Tous": None, "Réceptions": "RECEIVING", "Expéditions": "DELIVERY",
                "Retours": "RETURN", "Retours fournisseur": "SUPPLIER_RETURN",
                "Ajustements": "ADJUSTMENT",
                # Un jaugeage est un ADJUSTMENT, qu'il vienne d'une cuve
                # isolée (aucune référence) ou du relevé complet (référence
                # « Inventaire complet du ... ») : pas de préfixe, sinon la
                # moitié des jaugeages resterait invisible.
                "Jaugeages": "ADJUSTMENT",
                "Transferts régionaux": "REGIONAL_TRANSFER"}.get(filtre), None

    def _configurer_style_dates(self):
        """Habille les DateEntry aux couleurs sombres de l'application.

        tkcalendar dérive son style de celui d'une Combobox ttk ; on crée une
        variante "Dark.DateEntry" pour ne pas toucher aux autres widgets ttk
        (le tableau de l'historique en particulier).
        """
        style = ttk.Style(self)
        style.configure("Dark.DateEntry",
                        fieldbackground="#1a1d23", background="#2a2d35",
                        foreground="#e5e7eb", arrowcolor="#e5e7eb",
                        bordercolor="#2a2d35", lightcolor="#2a2d35",
                        darkcolor="#2a2d35")
        style.map("Dark.DateEntry",
                  fieldbackground=[("readonly", "#1a1d23"), ("disabled", "#15171c")],
                  foreground=[("readonly", "#e5e7eb"), ("disabled", "#6b7280")],
                  arrowcolor=[("disabled", "#4b5563")])

    def _plage_dates(self) -> tuple[str | None, str | None]:
        """Retourne (date_from, date_to) au format AAAA-MM-JJ.

        (None, None) quand la case "Filtrer par date" est décochée : c'est
        l'état "aucun filtre de date", les DateEntry affichant toujours une
        date même quand l'opérateur n'en veut pas.
        """
        if not self.date_filter_var.get():
            return None, None
        try:
            debut = self.from_date.get_date()
            fin = self.to_date.get_date()
        except (ValueError, AttributeError):
            return None, None
        return debut.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")

    def _on_date_selected(self, source: str):
        """Choisir une date active et applique le filtre immédiatement."""
        if not self.date_filter_var.get():
            self.date_filter_var.set(True)
        try:
            debut = self.from_date.get_date()
            fin = self.to_date.get_date()
        except (ValueError, AttributeError):
            self.refresh()
            return
        # Plage inversée : on aligne l'autre borne plutôt que de renvoyer un
        # tableau vide sans explication.
        if debut > fin:
            if source == "from":
                self.to_date.set_date(debut)
            else:
                self.from_date.set_date(fin)
        self.refresh()

    def _on_date_filter_toggle(self):
        self.refresh()

    def _clear_dates(self):
        """Remet les deux calendriers à aujourd'hui et coupe le filtre date."""
        today = date.today()
        self.date_filter_var.set(False)
        self.from_date.set_date(today)
        self.to_date.set_date(today)
        self.refresh()

    def _filter_local(self):
        query = self.search_var.get().strip().lower()
        operator = self.operator_var.get()
        use_operator = operator and operator != self._tous

        for item_id in list(self._bons_par_ligne.keys()):
            try:
                vals = self.tree.item(item_id, "values")
            except Exception:
                continue

            visible = True

            # Filtre par personne : un nom peut être le superviseur du bon
            # (`operator`) OU son saisisseur (`created_by`, qui n'a pas de
            # colonne visible). On accepte les deux, sinon choisir un
            # saisisseur vidait le tableau. Les deux valeurs sont lues dans
            # les données en mémoire et non par indice de colonne : le tableau
            # carburant n'a pas de colonne SUPERVISOR.
            if use_operator:
                info = self._bons_par_ligne.get(item_id) or {}
                if ((info.get("operator") or "") != operator
                        and (info.get("created_by") or "") != operator):
                    visible = False

            # Filtre par recherche texte
            if visible and query:
                text = " ".join(str(v).lower() for v in vals)
                if query not in text:
                    visible = False

            if visible:
                self.tree.reattach(item_id, "", "end")
            else:
                self.tree.detach(item_id)

    def _on_products(self, products):
        """Alimente le filtre produit une fois le catalogue reçu."""
        self._products = products
        self._product_map = {}
        names = [self._tous_produits]
        for p in products:
            label = f"{p['sku']} — {p['name']}"
            names.append(label)
            self._product_map[label] = p["id"]
        if self.winfo_exists():
            self.product_combo.configure(values=names)

    def _selected_product_id(self) -> int | None:
        return self._product_map.get(self.product_var.get())

    def _debounce_recherche(self):
        """Attend 500 ms sans frappe avant d'interroger le serveur : la
        recherche est désormais globale (tous les documents), un aller-
        retour par lettre tapée serait autant de requêtes inutiles."""
        if self._recherche_job:
            self.after_cancel(self._recherche_job)
        self._recherche_job = self.after(500, self.refresh)

    def refresh(self, garder_page: bool = False):
        if not garder_page:
            self._offset = 0
        self.status_label.configure(text=t("Chargement..."), text_color="#6b7280")
        if not self._products:
            # Le catalogue était chargé en direct sur le thread Tk : la fenêtre
            # restait figée le temps de l'appel HTTP. On le charge en tâche de
            # fond, puis on enchaîne sur les bons. Un échec ne bloque pas
            # l'historique : seul le filtre produit reste vide.
            run_async(
                self,
                self.api.get_products,
                lambda produits: (self._on_products(produits), self._charger_bons()),
                lambda _e: self._charger_bons(),
            )
            return
        self._charger_bons()

    def _charger_bons(self):
        if not self.winfo_exists():
            return
        doc_type, ref_prefix = self._selected_filter()
        date_from, date_to = self._plage_dates()
        product_id = self._selected_product_id()
        search_text = self.search_var.get().strip() or None
        operator_val = self.operator_var.get()
        operator_param = operator_val if operator_val and operator_val != self._tous else None

        run_async(
            self,
            lambda: self.api.list_documents(
                doc_type=doc_type, limit=self.PAGE, offset=self._offset,
                reference_prefix=ref_prefix,
                date_from=date_from, date_to=date_to,
                product_id=product_id,
                search=search_text, operator=operator_param),
            self._on_refresh_ok,
            lambda e: self.status_label.configure(text=str(e), text_color="orange"),
        )

    def _on_refresh_ok(self, result):
        documents, total = result
        self._total = total
        # Le filtre produit sélectionne des BONS (un bon avec au moins une
        # ligne du produit), pas des LIGNES : un inventaire complet qui
        # touche 40 produits reste sélectionné dès qu'il en contient un
        # seul recherché. Sans ce filtre supplémentaire, toutes ses lignes
        # s'affichaient — donnant l'impression que le filtre ne servait à
        # rien.
        filtre_product_id = self._selected_product_id()
        debut = self._offset + 1 if documents else 0
        fin = self._offset + len(documents)
        # Rappeler la plage appliquée : l'opérateur voit ainsi noir sur blanc
        # que le filtre de dates a bien été pris en compte.
        date_from, date_to = self._plage_dates()
        if date_from and date_to:
            d1 = datetime.strptime(date_from, "%Y-%m-%d").strftime("%d/%m/%Y")
            d2 = datetime.strptime(date_to, "%Y-%m-%d").strftime("%d/%m/%Y")
            suffixe = t(" — du {d1} au {d2}").format(d1=d1, d2=d2)
        else:
            suffixe = ""
        self.status_label.configure(
            text=(t("Bons {debut} à {fin} sur {total}").format(
                      debut=debut, fin=fin, total=total) + suffixe
                  if total else t("Aucun mouvement") + suffixe),
            text_color="gray",
        )
        self.bouton_precedent.configure(state="normal" if self._offset > 0 else "disabled")
        self.bouton_suivant.configure(state="normal" if fin < total else "disabled")

        self.tree.delete(*self.tree.get_children())
        self._bons_par_ligne = {}
        for doc in documents:
            type_label = {"RECEIVING": t("Réception"), "DELIVERY": t("Expédition"),
                          "RETURN": t("Retour"),
                          "SUPPLIER_RETURN": t("Retour fournisseur"),
                          "ADJUSTMENT": t("Ajustement"),
                          "REGIONAL_TRANSFER": t("Transfert régional"),
                          }.get(doc["type"], doc["type"])
            dt = _parse_date(doc["created_at"])
            date_label = f"{dt.month}/{dt.day}/{dt.year}" if dt else doc["created_at"]
            month_label = t(MONTHS_FR[dt.month - 1]) if dt else ""
            annule = bool(doc.get("cancelled_at"))
            statut = "ANNULÉ" if annule else _statut_valide(doc)
            statut_affiche = t(statut)
            commentaire = doc.get("note") or ""
            carrier = doc.get("carrier") or ""
            if carrier:
                commentaire = (t("[Transporteur : {carrier}]").format(
                    carrier=carrier) + " " + commentaire).strip()
            if annule:
                commentaire = (t("[Annulé : {motif}]").format(
                    motif=doc.get("cancel_reason") or t("sans motif"))
                    + " " + commentaire).strip()

            ref_display = doc.get("reference") or ""
            if annule:
                ref_display = f"⊘ {ref_display}"

            # Un transfert inter-régional porte sa destination dans `region`
            # et son expéditeur dans `source_region` : sans les afficher tous
            # les deux, la colonne Région ne montre que la moitié de « qui a
            # envoyé quoi à qui ».
            if doc["type"] == "REGIONAL_TRANSFER" and doc.get("source_region"):
                region_affichee = t("{src} → {dst}").format(
                    src=doc["source_region"], dst=doc.get("region") or "")
            else:
                region_affichee = doc.get("region") or ""

            lignes = doc["lines"]
            if filtre_product_id is not None:
                lignes = [l for l in lignes if l.get("product_id") == filtre_product_id]
            if annule:
                tags = ("annule",)
            elif statut == "En transit":
                tags = ("transit",)
            else:
                tags = ()
            mileage = doc.get("mileage")
            try:
                mileage_affiche = f"{float(mileage):g}" if mileage is not None else ""
            except (TypeError, ValueError):
                mileage_affiche = str(mileage)

            for line in lignes:
                # Valeurs par NOM de colonne : les colonnes diffèrent d'un
                # secteur à l'autre (voir `_build`), un tuple positionnel
                # unique ne pouvait plus convenir.
                # Pour un ajustement (inventaire/jaugeage), la colonne Qté
                # montre ce qui a été COMPTÉ — pas l'écart signé, qui se lit
                # comme une quantité expédiée/reçue alors que ce n'en est pas
                # une. Longtemps réservé à Fuel, généralisé à tous les
                # secteurs le 13 septembre 2026 (même confusion signalée
                # pour un inventaire RAN : « je veux ce que j'ai compté, pas
                # un rapport d'écart comme pour une expédition »).
                qte_affichee = line["quantity"]
                if doc["type"] == "ADJUSTMENT" and line.get("counted_quantity") is not None:
                    qte_affichee = line["counted_quantity"]
                valeurs = {
                    "statut": statut_affiche, "type": type_label,
                    "location": "TP WH", "item": line["name"],
                    "part_number": line["sku"], "date": date_label,
                    "month": month_label, "qty": qte_affichee,
                    "receipt": ref_display, "receiver": doc["party"] or "",
                    "region": region_affichee, "supervisor": doc["operator"] or "",
                    "comments": commentaire,
                    "plate": doc.get("vehicle_plate") or "",
                    "vehicle": doc.get("vehicle_info") or "",
                    "project": doc.get("project") or "",
                    "mileage": mileage_affiche,
                    "fuel_card": doc.get("fuel_card") or "",
                }
                # Pour le carburant, la colonne « Receveur » vient du champ
                # dédié quand il est renseigné (le formulaire recopie `party`
                # dans `receiver`, mais l'historique ancien peut n'avoir que
                # l'un des deux).
                if self._is_fuel:
                    valeurs["receiver"] = (doc.get("receiver")
                                           or doc.get("party") or "")
                item_id = self.tree.insert(
                    "", "end", tags=tags,
                    values=tuple(valeurs.get(col, "") for col in self._colonnes))
                self._bons_par_ligne[item_id] = {"id": doc["id"], "annule": annule,
                                                  "type": type_label,
                                                  "doc_type": doc["type"],
                                                  "party": doc.get("party") or "",
                                                  "region": doc.get("region") or "",
                                                  "operator": doc.get("operator") or "",
                                                  "created_by": doc.get("created_by") or "",
                                                  "created_at": doc.get("created_at") or "",
                                                  "note": doc.get("note") or "",
                                                  "carrier": doc.get("carrier") or "",
                                                  "lines": doc.get("lines", []),
                                                  # Le bon complet tel que le
                                                  # serveur l'a renvoyé : c'est
                                                  # lui, et non le treeview, qui
                                                  # alimente le PDF.
                                                  "bon": doc}

        # Collecter les opérateurs pour le filtre
        ops_set: set[str] = set()
        for doc in documents:
            op = doc.get("operator") or ""
            if op:
                ops_set.add(op)
            cb = doc.get("created_by") or ""
            if cb and cb != op:
                ops_set.add(cb)
        for op in sorted(ops_set):
            if op not in self._known_operators:
                self._known_operators.append(op)
        self.operator_combo.configure(values=[self._tous] + self._known_operators)

        # Réappliquer les filtres locaux (recherche texte + opérateur) : sans
        # cela ils restaient affichés à l'écran sans plus rien filtrer après un
        # rafraîchissement, un changement de type ou une pagination.
        self._filter_local()

        self._mettre_a_jour_action_annulation()

    def _mettre_a_jour_action_annulation(self, _event=None):
        """N'autorise l'action que pour un seul bon actif sélectionné."""
        selection = self.tree.selection()
        bons = {self._bons_par_ligne[i]["id"] for i in selection if i in self._bons_par_ligne}

        if self._lecture_seule:
            # Les boutons Annuler / Dupliquer ne sont pas affichés : seule
            # l'impression reste, et le libellé d'aide doit parler d'elle.
            un_seul = len(bons) == 1
            self.bouton_imprimer.configure(state="normal" if un_seul else "disabled")
            self.bouton_photos.configure(state="normal" if un_seul else "disabled")
            if len(bons) > 1:
                aide = t("Sélectionne les lignes d'un seul bon à la fois.")
            elif un_seul:
                aide = t("Imprimer le bon n°{id} sélectionné.").format(
                    id=next(iter(bons)))
            else:
                aide = t("Sélectionne une ligne pour l'imprimer.")
            self.aide_annulation.configure(text=aide)
            return

        if not bons:
            self.bouton_annuler.configure(state="disabled")
            self.bouton_imprimer.configure(state="disabled")
            self.bouton_photos.configure(state="disabled")
            self.bouton_dupliquer.configure(state="disabled")
            self.aide_annulation.configure(
                text=t("Sélectionne une ligne pour annuler son bon."))
        elif len(bons) > 1:
            self.bouton_annuler.configure(state="disabled")
            self.bouton_imprimer.configure(state="disabled")
            self.bouton_photos.configure(state="disabled")
            self.bouton_dupliquer.configure(state="disabled")
            self.aide_annulation.configure(
                text=t("Sélectionne les lignes d'un seul bon à la fois."))
        else:
            self.bouton_imprimer.configure(state="normal")
            self.bouton_photos.configure(state="normal")
            info = self._bons_par_ligne[selection[0]]
            # Dupliquer est possible pour les types RECEIVING, DELIVERY, RETURN
            if info.get("doc_type") in ("RECEIVING", "DELIVERY", "RETURN"):
                self.bouton_dupliquer.configure(state="normal")
            else:
                self.bouton_dupliquer.configure(state="disabled")
            if info["annule"]:
                self.bouton_annuler.configure(state="disabled")
                self.aide_annulation.configure(
                    text=t("Le bon n°{id} est déjà annulé.").format(id=info["id"]))
            else:
                self.bouton_annuler.configure(state="normal")
                self.aide_annulation.configure(
                    text=t("Annuler le bon n°{id} sélectionné.").format(id=info["id"]))

    def _page_suivante(self):
        if self._offset + self.PAGE < self._total:
            self._offset += self.PAGE
            self.refresh(garder_page=True)

    def _page_precedente(self):
        if self._offset > 0:
            self._offset = max(0, self._offset - self.PAGE)
            self.refresh(garder_page=True)

    def _annuler_bon(self):
        if self._lecture_seule:
            return
        selection = self.tree.selection()
        if not selection:
            messagebox.showinfo(
                t("Annulation"),
                t("Sélectionne d'abord une ligne du bon à annuler."))
            return

        bons = {self._bons_par_ligne[i]["id"] for i in selection if i in self._bons_par_ligne}
        if len(bons) > 1:
            messagebox.showwarning(
                t("Annulation"),
                t("La sélection porte sur plusieurs bons différents.\n"
                  "Sélectionne des lignes d'un seul bon à la fois."),
            )
            return

        info = self._bons_par_ligne[selection[0]]
        if info["annule"]:
            messagebox.showinfo(
                t("Annulation"),
                t("Le bon n°{id} est déjà annulé.").format(id=info["id"]))
            return

        motif = self._demander_motif(info)
        if not motif:
            return

        # Appel réseau en tâche de fond : un serveur lent ou injoignable ne
        # doit pas figer toute la fenêtre (même défaut déjà corrigé ailleurs
        # dans les écrans du quotidien — voir document_form/inventory).
        self.bouton_annuler.configure(state="disabled", text=t("Annulation…"))

        def _ok(_res):
            if self.winfo_exists():
                self.bouton_annuler.configure(state="normal", text=t("Annuler le bon"))
            messagebox.showinfo(
                t("Bon annulé"),
                t("{type} n°{id} annulée.\nLe stock a été rétabli et le bon reste "
                  "tracé dans l'historique.").format(type=info["type"], id=info["id"]),
            )
            self.refresh()

        def _err(e):
            if self.winfo_exists():
                self.bouton_annuler.configure(state="normal", text=t("Annuler le bon"))
            messagebox.showerror(t("Annulation impossible"), str(e))

        run_async(self,
                  lambda: self.api.cancel_document(info["id"], motif, self._operateur()),
                  _ok, _err)

    def _demander_motif(self, info: dict) -> str | None:
        """Demande le motif ; None si l'opérateur renonce ou n'en saisit pas."""
        dialogue = ctk.CTkInputDialog(
            title=t("Annuler le bon"),
            text=t("Annulation de {type} n°{id}.\nLe mouvement de stock sera "
                   "inversé.\n\nMotif (obligatoire, 3 caractères minimum) :").format(
                       type=info["type"].lower(), id=info["id"]),
        )
        motif = (dialogue.get_input() or "").strip()
        if not motif:
            return None
        if len(motif) < 3:
            messagebox.showwarning(
                t("Motif trop court"),
                t("Le motif doit faire au moins 3 caractères."))
            return None
        return motif

    def _operateur(self) -> str:
        try:
            from config import load_config
            return load_config().get("operator", "")
        except Exception:
            return ""

    def _photos_bon(self):
        """Ouvre les pièces jointes du bon sélectionné."""
        selection = self.tree.selection()
        if not selection:
            return
        info = self._bons_par_ligne.get(selection[0])
        if not info:
            return
        PhotosBonDialog(self, self.api, info["id"],
                        lecture_seule=self._lecture_seule)

    def _imprimer_bon(self):
        """Ticket PDF du bon sélectionné, prêt à envoyer (WhatsApp, e-mail).

        Tout vient des données EN MÉMOIRE (`self._bons_par_ligne[...]["bon"]`,
        le bon tel que le serveur l'a renvoyé), jamais des cases du tableau :
        les colonnes affichées changent selon le secteur, et un filtre actif
        détache des lignes — les lire par indice donnait un PDF incomplet ou
        décalé. La mise en page est celle de `views/print_utils.py`, partagée
        avec l'impression proposée juste après une saisie.
        """
        selection = self.tree.selection()
        if not selection:
            return
        info = self._bons_par_ligne.get(selection[0])
        if not info:
            return
        bon = info.get("bon") or {}
        if not (bon.get("lines") or info.get("lines")):
            return
        unites = {p["id"]: p.get("unit", "") for p in self._products}
        try:
            chemin = generer_pdf_bon(bon, unites,
                                     parent=self.winfo_toplevel(),
                                     secteur=self.sector)
        except (OSError, PermissionError) as e:
            # Cas courant : le PDF précédent est resté ouvert dans le lecteur.
            messagebox.showerror(
                t("PDF"),
                t("Impossible d'écrire le fichier :\n{e}\n\nVérifie qu'il "
                  "n'est pas déjà ouvert dans un autre programme.").format(e=e))
            return
        if not chemin:
            return
        messagebox.showinfo(t("PDF"), t("Bon n°{id} exporté vers {path}").format(
            id=info["id"], path=chemin))

    def _dupliquer_bon(self):
        """Duplique le bon selectionne en naviguant vers le formulaire correspondant."""
        if self._lecture_seule:
            return
        selection = self.tree.selection()
        if not selection:
            return
        info = self._bons_par_ligne.get(selection[0])
        if not info:
            return
        if info["annule"]:
            messagebox.showinfo(t("Bon annulé"), t("Ce bon est déjà annulé."))
            return
        doc_type = info.get("doc_type")
        if doc_type not in ("RECEIVING", "DELIVERY", "RETURN"):
            messagebox.showinfo(
                t("Duplication"),
                t("Seuls les bons de type réception, expédition ou retour "
                  "peuvent être dupliqués."))
            return
        bon_data = {
            "doc_type": doc_type,
            "party": info.get("party", ""),
            "region": info.get("region", ""),
            "operator": info.get("operator", ""),
            "carrier": info.get("carrier", ""),
            "lines": info.get("lines", []),
        }
        # Remonter a l'application principale pour naviguer
        app = self.winfo_toplevel()
        if hasattr(app, "dupliquer_bon"):
            app.dupliquer_bon(bon_data)

    def _export_pdf(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
            initialfile="historique.pdf")
        if not path:
            return

        pdf = FPDF(orientation="L", format="A4")
        font = setup_pdf_fonts(pdf)
        pdf.set_auto_page_break(auto=True, margin=15)
        pdf.add_page()

        T = lambda texte: safe_text(texte, font)

        def tronquer(texte: str, largeur_mm: float) -> str:
            """Raccourcit `texte` (avec « … ») pour qu'il tienne DANS la
            cellule, au lieu d'une troncature à taille de caractères fixe
            qui laissait le texte long déborder par-dessus la cellule
            suivante — illisible dès qu'une référence ou un motif dépassait
            30 caractères (ex. « Inventaire complet du 13/09/2026 »)."""
            texte = str(texte)
            marge_utile = max(largeur_mm - 2, 4)
            if pdf.get_string_width(texte) <= marge_utile:
                return texte
            while texte and pdf.get_string_width(texte + "…") > marge_utile:
                texte = texte[:-1]
            return (texte + "…") if texte else ""

        pdf.set_font(font, "B", 14)
        # L'écran Historique distingue déjà "Inventaires" des autres bons
        # (filtre dédié) : l'export doit suivre la même distinction plutôt
        # que de forcer un inventaire dans le gabarit "bon d'expédition"
        # (colonne Tiers vide, écart affiché comme une quantité livrée) —
        # signalé en réel le 13 septembre 2026, secteur RAN.
        _, ref_prefix_actif = self._selected_filter()
        est_inventaire = ref_prefix_actif == "Inventaire complet"
        titre = t("Rapport d'inventaire") if est_inventaire else t("Historique des mouvements")
        pdf.cell(0, 10, T(titre), new_x="LMARGIN", new_y="NEXT")
        # Ce PDF ne contient QUE les lignes affichées à l'écran (page courante,
        # filtres locaux compris). Annoncer « N bons au total » — le total
        # serveur — laissait croire à un export complet de l'historique.
        lignes_visibles = self.tree.get_children()
        pdf.set_font(font, "", 8)
        pdf.cell(0, 5, T(t("Exporté le {date} — {n} ligne(s) affichée(s) "
                           "sur {total} bon(s) au total").format(
            date=date.today().strftime("%d/%m/%Y"),
            n=len(lignes_visibles), total=self._total)),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)

        # Colonnes désignées par leur NOM et non par leur indice : le tableau
        # carburant n'a ni Part Number ni SUPERVISOR, et un export indexé en
        # dur y aurait imprimé les mauvaises cases.
        if est_inventaire:
            # Pas de "Type" (toujours "Ajustement") ni de "Tiers" (vide pour
            # un inventaire) : à la place, la référence complète (motif du
            # comptage) et l'opérateur, sur des colonnes assez larges pour
            # ne pas être tronquées.
            noms = ["statut", "item", "part_number", "qty", "date",
                    "receipt", "supervisor"]
            headers = [T(t("Statut")), T(t("Produit")), "SKU",
                       T(t("Compté")), T(t("Date")), T(t("Référence")),
                       T(t("Opérateur"))]
            col_w = [20, 65, 32, 20, 22, 68, 50]
        elif self._is_fuel:
            noms = ["statut", "type", "item", "date", "qty", "receipt",
                    "receiver", "plate", "project"]
            headers = [T(t("Statut")), T(t("Type")), T(t("Cuve")),
                       T(t("Date")), T(t("Qté (gls)")), T(t("N° Bon")),
                       T(t("Receveur")), T(t("Plaque")), T(t("Projet"))]
            col_w = [18, 22, 55, 28, 22, 16, 22, 40, 40]
        else:
            noms = ["statut", "type", "item", "part_number", "date", "qty",
                    "receipt", "receiver", "supervisor"]
            headers = [T(t("Statut")), T(t("Type")), T(t("Produit")), "SKU",
                       T(t("Date")), T(t("Qté")), T(t("Réf")), T(t("Tiers")),
                       T(t("Opérateur"))]
            col_w = [18, 22, 55, 28, 22, 16, 22, 40, 40]
        indices = [self._colonnes.index(nom) for nom in noms]
        pdf.set_font(font, "B", 7)
        for i, h in enumerate(headers):
            pdf.cell(col_w[i], 6, h, border=1)
        pdf.ln()

        pdf.set_font(font, "", 7)
        for item in lignes_visibles:
            vals = self.tree.item(item, "values")
            row = [str(vals[i]) for i in indices]
            for i, cell in enumerate(row):
                pdf.cell(col_w[i], 5, T(tronquer(cell, col_w[i])), border=1)
            pdf.ln()

        try:
            pdf.output(path)
        except (OSError, PermissionError) as e:
            messagebox.showerror(
                t("Export PDF"),
                t("Impossible d'écrire le fichier :\n{e}\n\nVérifie qu'il "
                  "n'est pas déjà ouvert dans un autre programme.").format(e=e))
            return
        messagebox.showinfo(t("Export PDF"),
                            t("PDF exporté vers {path}").format(path=path))

    def _filtres_locaux_actifs(self) -> list[str]:
        """Filtres appliqués à l'écran seulement, jamais transmis au serveur.

        Type, produit et période sont des filtres serveur : l'export Excel les
        reprend. La recherche texte et le choix d'une personne, eux, ne font que
        masquer des lignes du tableau — un export lancé depuis cet écran
        contient donc PLUS que ce qui est affiché. Le dire avant d'écrire le
        fichier évite de diffuser un export qu'on croyait restreint.
        """
        actifs = []
        if self.search_var.get().strip():
            actifs.append(t("la recherche « {q} »").format(
                q=self.search_var.get().strip()))
        operateur = self.operator_var.get()
        if operateur and operateur != self._tous:
            actifs.append(t("la personne « {op} »").format(op=operateur))
        return actifs

    def _export(self):
        locaux = self._filtres_locaux_actifs()
        if locaux and not messagebox.askyesno(
            t("Export Excel"),
            t("L'export reprend le type, le produit et la période, mais PAS "
              "{filtres} : le fichier contiendra plus de lignes que le "
              "tableau affiché.\n\nContinuer ?").format(
                  filtres=t(" ni ").join(locaux))):
            return
        path = filedialog.asksaveasfilename(defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")], initialfile="historique.xlsx")
        if not path:
            return
        try:
            doc_type, ref_prefix = self._selected_filter()
            date_from, date_to = self._plage_dates()
            product_id = self._selected_product_id()
            infos = self.api.export_documents(path, doc_type=doc_type,
                                               date_from=date_from, date_to=date_to,
                                               product_id=product_id,
                                               reference_prefix=ref_prefix)
            if (infos or {}).get("tronque"):
                # Un fichier amputé qui ne le dit pas fausse tout total calculé
                # dessus : l'opérateur doit resserrer ses filtres.
                messagebox.showwarning(
                    t("Export incomplet"),
                    t("Seuls les {n} bons les plus récents ont été exportés "
                      "sur {total} au total.\n\nRestreins la période ou le "
                      "type de bon pour obtenir un export complet.").format(
                        n=infos.get("exportes", 0), total=infos.get("total", 0)))
            else:
                messagebox.showinfo(
                    t("Export"),
                    t("Historique exporté vers {path}").format(path=path))
        except ApiError as e:
            messagebox.showerror(t("Erreur"), str(e))
        except (OSError, PermissionError) as e:
            # Cas courant : le fichier est déjà ouvert dans Excel.
            messagebox.showerror(
                t("Erreur"),
                t("Impossible d'écrire le fichier :\n{e}\n\nVérifie qu'il "
                  "n'est pas déjà ouvert dans un autre programme.").format(e=e))

    # --- Mises à jour temps réel (WebSocket) ---
    def _rafraichir_temps_reel(self):
        """Rafraîchit l'historique suite à un événement serveur.

        Deux précautions : `garder_page=True` — un refresh() nu remettait
        l'opérateur en page 1 alors qu'il consultait la page 4 ; et
        `winfo_ismapped()` — inutile d'interroger le serveur pour un écran que
        personne ne regarde, il sera rechargé à son affichage.
        """
        if not self.winfo_exists() or not self.winfo_ismapped():
            return
        self.refresh(garder_page=True)

    def document_cree(self, document_data):
        """Un bon vient d'être créé par un autre poste."""
        self._rafraichir_temps_reel()

    def document_annule(self, document_data):
        """Un bon vient d'être annulé par un autre poste."""
        self._rafraichir_temps_reel()


class PhotosBonDialog(ctk.CTkToplevel):
    """Pièces jointes d'un bon : colis abîmé, bordereau signé, preuve de livraison.

    Plusieurs photos par bon, délibérément : un dégât se photographie sous
    plusieurs angles, et l'étiquette du colis ne remplace pas la photo du
    dégât. Chaque vignette est chargée séparément — vingt photos ne doivent pas
    faire attendre l'ouverture du dialogue avant d'en montrer une seule.

    Un lecteur consulte mais n'ajoute pas : le serveur refuserait l'envoi (403),
    autant ne pas lui montrer le bouton.
    """

    def __init__(self, master, api, document_id: int, lecture_seule: bool = False):
        super().__init__(master)
        self.api = api
        self.document_id = document_id
        self._lecture_seule = lecture_seule
        # Les CTkImage doivent survivre à la fin de la méthode qui les crée :
        # Tk ne garde pas de référence, l'image disparaîtrait du cadre.
        self._images: list = []

        self.title(t("Photos du bon n°{id}").format(id=document_id))
        self.geometry("620x520")
        self.transient(master)
        self.lift()
        self.grab_set()

        ctk.CTkLabel(self, text=t("Pièces jointes"),
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(14, 2))
        ctk.CTkLabel(
            self,
            text=t("Photos rattachées à ce bon : colis abîmé, bordereau "
                   "signé, état de la marchandise à l'arrivée."),
            text_color="#6b7280", font=ctk.CTkFont(size=11),
            wraplength=560).pack(pady=(0, 8))

        self._cadre = ctk.CTkScrollableFrame(self, fg_color="#1a1d23",
                                             corner_radius=10)
        self._cadre.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        self._status = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11),
                                    text_color="#6b7280", wraplength=560)
        self._status.pack(padx=16)

        boutons = ctk.CTkFrame(self, fg_color="transparent")
        boutons.pack(fill="x", padx=16, pady=(4, 12))
        ctk.CTkButton(boutons, text=t("Fermer"), width=110, fg_color="gray30",
                      command=self.destroy).pack(side="right", padx=6)
        if not self._lecture_seule and PHOTOS_DISPONIBLES:
            ctk.CTkButton(boutons, text=t("📷  Ajouter une photo"), width=190,
                          fg_color="#2563eb", hover_color="#1d4ed8",
                          command=self._ajouter).pack(side="right")

        self.refresh()

    def refresh(self):
        self._status.configure(text=t("Chargement…"), text_color="#6b7280")
        run_async(self,
                  lambda: self.api.get_document_photos(self.document_id),
                  self._afficher, self._erreur)

    def _erreur(self, exc):
        if self.winfo_exists():
            self._status.configure(text=str(exc), text_color="orange")

    def _afficher(self, photos_du_bon):
        if not self.winfo_exists():
            return
        for enfant in self._cadre.winfo_children():
            enfant.destroy()
        self._images = []

        if not photos_du_bon:
            ctk.CTkLabel(self._cadre, text=t("Aucune photo pour ce bon."),
                         text_color="#6b7280").pack(anchor="w", padx=12, pady=12)
            self._status.configure(text="", text_color="#6b7280")
            return

        for photo in photos_du_bon:
            self._ligne_photo(photo)
        self._status.configure(
            text=t("{n} photo(s)").format(n=len(photos_du_bon)),
            text_color="#6b7280")

    def _ligne_photo(self, photo: dict):
        ligne = ctk.CTkFrame(self._cadre, fg_color="transparent")
        ligne.pack(fill="x", padx=8, pady=6)

        vignette = ctk.CTkLabel(ligne, text=t("Chargement…"), width=140,
                                height=110, text_color="#6b7280",
                                font=ctk.CTkFont(size=10))
        vignette.pack(side="left", padx=(0, 10))

        infos = ctk.CTkFrame(ligne, fg_color="transparent")
        infos.pack(side="left", fill="x", expand=True, anchor="n")
        if photo.get("caption"):
            ctk.CTkLabel(infos, text=photo["caption"], text_color="#d1d5db",
                         anchor="w", wraplength=380,
                         justify="left").pack(anchor="w")
        ctk.CTkLabel(
            infos,
            text=t("Ajoutée par {qui} le {quand}").format(
                qui=photo.get("uploaded_by") or "—",
                quand=(photo.get("created_at") or "")[:16]),
            text_color="#6b7280", font=ctk.CTkFont(size=10)).pack(anchor="w")

        def _ok(octets):
            if not vignette.winfo_exists():
                return
            image = miniature(octets, 140, 110) if octets else None
            if image is None:
                vignette.configure(image=None, text=t("Photo illisible"))
                return
            self._images.append(image)
            vignette.configure(image=image, text="")

        def _err(_exc):
            if vignette.winfo_exists():
                vignette.configure(image=None, text=t("Photo indisponible"))

        run_async(self,
                  lambda pid=photo["id"]: self.api.get_document_photo(
                      self.document_id, pid),
                  _ok, _err)

    def _ajouter(self):
        chemin = choisir_image(self, t("Photo à joindre au bon"))
        if not chemin:
            return
        legende = SaisieLegendeDialog(self).resultat
        if legende is None:  # annulé
            return
        self._status.configure(text=t("Envoi…"), text_color="#6b7280")

        def _ok(_res):
            if self.winfo_exists():
                self.refresh()

        def _err(exc):
            if self.winfo_exists():
                self._status.configure(text="", text_color="#6b7280")
                messagebox.showerror(t("Photo refusée"), str(exc), parent=self)

        run_async(self,
                  lambda: self.api.upload_document_photo(
                      self.document_id, chemin, legende),
                  _ok, _err)


class SaisieLegendeDialog(ctk.CTkToplevel):
    """Légende facultative de la photo, en une ligne.

    Bloquant volontairement (`wait_window`) : l'envoi n'a de sens qu'une fois
    la légende connue, et enchaîner deux fenêtres non modales ferait perdre le
    fil au magasinier.
    """

    def __init__(self, master):
        super().__init__(master)
        self.resultat = None
        self.title(t("Légende de la photo"))
        self.geometry("420x180")
        self.resizable(False, False)
        self.transient(master)
        self.lift()
        self.grab_set()

        ctk.CTkLabel(self, text=t("Légende (facultative)"),
                     font=ctk.CTkFont(size=13, weight="bold")).pack(pady=(16, 4))
        self._var = tk.StringVar()
        entree = ctk.CTkEntry(self, textvariable=self._var, width=360,
                              placeholder_text=t("Ex. : carton éventré côté droit"))
        entree.pack(padx=20, pady=(0, 12))
        entree.bind("<Return>", lambda _e: self._valider())
        entree.focus_set()

        boutons = ctk.CTkFrame(self, fg_color="transparent")
        boutons.pack(pady=(0, 12))
        ctk.CTkButton(boutons, text=t("Annuler"), width=110, fg_color="gray30",
                      command=self.destroy).pack(side="left", padx=6)
        ctk.CTkButton(boutons, text=t("Envoyer"), width=140,
                      fg_color="#2563eb", hover_color="#1d4ed8",
                      command=self._valider).pack(side="left", padx=6)

        self.wait_window()

    def _valider(self):
        # Chaîne vide = pas de légende, mais envoi confirmé. None = annulation :
        # les deux ne doivent pas se confondre côté appelant.
        self.resultat = self._var.get().strip()
        self.destroy()
