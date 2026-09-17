import tkinter as tk
import uuid
from datetime import datetime, timedelta
from tkinter import messagebox, ttk

import customtkinter as ctk

from api_client import ApiError
from async_call import run_async
from i18n import t
from document_lines import (LignesBon, chercher_par_sku, resumer_lignes,
                            texte_resume)
from file_attente import ajouter as mettre_en_attente
from local_cache import (charger_brouillon, charger_contacts, charger_produits,
                         effacer_brouillon, sauvegarder_brouillon,
                         sauvegarder_contacts, sauvegarder_produits)
from operator_context import remplacer_operateur_defaut
from recherche import correspond_libelle, normaliser
from reference_data import (regions, regions_for_supervisor, supervisors_for,
                            technicians, warehouse_operators)
from views.theme import SUCCESS, DANGER
from views.utils import float_saisie

# Choix explicite dans la liste Technicien : un ComboBox vide laisse
# croire à un champ non chargé, alors que la plupart des expéditions
# n'ont effectivement aucun technicien nommé.
AUCUN_TECHNICIEN = "Aucun"

TYPE_LABELS = {
    "RECEIVING": {
        "title": "Réception",
        "icon": "🚚",
        "party": "Source",
        "operator": "Opérateur",
        "reference": "N° Bordereau",
        "verb": "✓  Enregistrer la réception",
        "color": "#22c55e",
        "hover": "#16a34a",
    },
    "DELIVERY": {
        "title": "Expédition",
        "icon": "📤",
        "party": "Réceptionnaire",
        "operator": "Superviseur",
        "reference": "N° Reçu",
        "verb": "✓  Enregistrer l'expédition",
        "color": "#f59e0b",
        "hover": "#d97706",
    },
    "RETURN": {
        "title": "Retour",
        "icon": "🔄",
        "party": "Provenance",
        "operator": "Superviseur",
        "reference": "N° Retour",
        "verb": "✓  Enregistrer le retour",
        "color": "#3b82f6",
        "hover": "#2563eb",
    },
}


class DocumentFormView(ctk.CTkFrame):
    def __init__(self, master, api, doc_type: str, default_operator: str = "",
                 on_queue_change=None, sector: str | None = None,
                 demo_mode: bool = False):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self.doc_type = doc_type
        self.labels = TYPE_LABELS[doc_type]
        self.sector = sector
        # Mode démo (voir server/app.py::login) : remplace des noms réels
        # (fournisseur, entrepôt) par des libellés génériques dans les
        # suggestions par défaut, pour des captures publiques présentables
        # à un public international sans jamais changer l'usage réel.
        self.demo_mode = demo_mode
        # Fuel n'a ni région ni superviseur (une seule cuve centrale) : son
        # bon s'organise autour d'un véhicule et d'un projet à la place. Voir
        # `_build` pour le formulaire alternatif, et `_submit` pour la
        # validation/le payload correspondants.
        self._is_fuel = sector == "FUEL"
        self._is_fuel_receiving = self._is_fuel and doc_type == "RECEIVING"
        self.products: list[dict] = []
        # Nom de contact -> id : alimenté seulement pour RECEIVING (voir
        # refresh_products). Vide sinon, party_contact_id reste alors None.
        self._contacts_par_nom: dict[str, int] = {}
        # Formes traduites des libellés qui servent aussi de sentinelles.
        # Calculées ici et non au niveau module : la langue n'est fixée
        # qu'après l'import des vues (voir main.py).
        self._aucun_technicien = t(AUCUN_TECHNICIEN)
        self._prefixe_bidons = t("bidons")
        self.lines = LignesBon()
        self._cle_idempotence: str | None = None
        self._on_queue_change = on_queue_change
        self._brouillon_propose = False
        self._toast_after_id = None
        self._build(default_operator)
        # Laisse la fenêtre s'afficher avant de poser la question : une
        # boîte de dialogue pendant la construction de la vue reste
        # invisible derrière la fenêtre principale.
        self.after(600, self._proposer_reprise_brouillon)

    def _build(self, default_operator: str):
        title_frame = ctk.CTkFrame(self, fg_color="transparent")
        title_frame.pack(fill="x", padx=20, pady=(20, 8))
        ctk.CTkLabel(title_frame,
                     text=f"{self.labels['icon']}  {t(self.labels['title'])}",
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")

        # Pas de bouton « Importer Excel » ici : l'import de bons de réception
        # par fichier n'existe pas côté serveur. Le catalogue produits, lui,
        # s'importe depuis l'écran Produits.

        header = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                              border_width=1, border_color="#2a2d35")
        header.pack(fill="x", padx=20, pady=4)

        self.party_var = tk.StringVar()
        # `default_operator` est le nom de l'opérateur entrepôt connecté : il
        # ne s'applique qu'à la Réception. En Expédition/Retour, ce champ
        # désigne le superviseur terrain — un nom totalement différent, à
        # choisir explicitement dans la liste de la région.
        self.operator_var = tk.StringVar(
            value=default_operator if self.doc_type == "RECEIVING" else "")
        self.region_var = tk.StringVar()
        self.reference_var = tk.StringVar()
        self.note_var = tk.StringVar()
        self.transporteur_var = tk.StringVar()
        # Technicien destinataire (expédition uniquement, facultatif) : sert au
        # suivi nominatif des bouteilles consignées.
        self.technicien_var = tk.StringVar()
        self.date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d %H:%M"))
        # Champs Fuel (secteur FUEL uniquement, voir self._is_fuel) : sans
        # région ni superviseur, une livraison de carburant s'organise autour
        # d'un véhicule et d'un « projet » (étiquette budgétaire interne).
        self.vehicle_plate_var = tk.StringVar()
        self.project_var = tk.StringVar()
        self.mileage_var = tk.StringVar()
        self.fuel_card_var = tk.StringVar()

        fields_row1 = ctk.CTkFrame(header, fg_color="transparent")
        fields_row1.pack(fill="x", padx=12, pady=(10, 4))
        for i in range(3):
            fields_row1.grid_columnconfigure(i, weight=1, uniform="f")

        if self._is_fuel_receiving:
            # Réception carburant : fournisseur, plaque camion-citerne, opérateur
            self._sources_defaut = (
                ["Gulf Oil", "National Petroleum", "Supplier"]
                if self.demo_mode else ["Supplier"])
            self.source_combo = self._field_combo_editable(
                fields_row1, t("Fournisseur"),
                self.party_var, self._sources_defaut, 0, 0)
            self.vehicle_combo = self._field_combo_editable(
                fields_row1, t("Plaque camion-citerne"),
                self.vehicle_plate_var, [], 0, 1)
            self.operator_combo = self._field_combo(
                fields_row1, t(self.labels["operator"]),
                self.operator_var, warehouse_operators(), 0, 2)
            self.region_combo = None
            self.project_combo = None
        elif self.doc_type == "RECEIVING":
            self._sources_defaut = (
                ["Central Supply", "Supplier"] if self.demo_mode
                else ["Digicel WH", "Supplier"])
            self.source_combo = self._field_combo(fields_row1, t(self.labels["party"]),
                              self.party_var, self._sources_defaut, 0, 0)
            self.region_var.set("Central WH" if self.demo_mode else "TP-WH")
            self._field(fields_row1, t("Destination"), self.region_var, 0, 1,
                        readonly=True)
            self.region_combo = None
            self.operator_combo = self._field_combo(
                fields_row1, t(self.labels["operator"]),
                self.operator_var, warehouse_operators(), 0, 2)
        elif self._is_fuel:
            # Ni région ni superviseur pour Fuel : le véhicule et le projet
            # remplacent ce rôle. « Receveur » réutilise le champ party
            # existant (déjà du texte libre par type de bon), plutôt que
            # d'ajouter un widget redondant pour la même information.
            self._field(fields_row1, t("Receveur"), self.party_var, 0, 0)
            self.vehicle_combo = self._field_combo_editable(
                fields_row1, t("Plaque du véhicule"), self.vehicle_plate_var,
                [], 0, 1)
            self.project_combo = self._field_combo_editable(
                fields_row1, t("Projet"), self.project_var, [], 0, 2)
            self.region_combo = None
            self.operator_combo = None
            self._sources_defaut = None
            self.source_combo = None
        else:
            self._field(fields_row1, t(self.labels["party"]), self.party_var, 0, 0)
            self.region_combo = self._field_combo(fields_row1, t("Région"),
                                                  self.region_var, [""] + regions(), 0, 1,
                                                  command=self._on_region_change)
            self.operator_combo = self._field_combo(
                fields_row1, t(self.labels["operator"]),
                self.operator_var, supervisors_for(""), 0, 2,
                                                    command=self._on_supervisor_change)
            self._sources_defaut = None
            self.source_combo = None

        fields_row2 = ctk.CTkFrame(header, fg_color="transparent")
        fields_row2.pack(fill="x", padx=12, pady=(0, 10))
        n_cols = 5 if (self.doc_type == "DELIVERY" or self._is_fuel) and not self._is_fuel_receiving else 3
        for i in range(n_cols):
            fields_row2.grid_columnconfigure(i, weight=1, uniform="f")

        self._field(fields_row2, t(self.labels["reference"]), self.reference_var, 0, 0)
        self._field(fields_row2, t("Date"), self.date_var, 0, 1)
        if self._is_fuel and not self._is_fuel_receiving:
            self.technicien_combo = None
            self._field(fields_row2, t("Kilométrage"), self.mileage_var, 0, 2)
            self._field(fields_row2, t("Carte carburant"), self.fuel_card_var, 0, 3)
            self._field(fields_row2, t("Note"), self.note_var, 0, 4)
        elif self.doc_type == "DELIVERY":
            self._field(fields_row2, t("Transporté par"), self.transporteur_var, 0, 2)
            # Facultatif : « Aucun » (valeur vide) pour une livraison
            # régionale ordinaire, qui n'est adressée à personne en propre.
            self.technicien_combo = self._field_combo(
                fields_row2, t("Technicien (facultatif)"), self.technicien_var,
                [self._aucun_technicien] + technicians(), 0, 3,
                command=self._on_technicien_change)
            self.technicien_var.set(self._aucun_technicien)
            self._field(fields_row2, t("Note"), self.note_var, 0, 4)
        else:
            self.technicien_combo = None
            self._field(fields_row2, t("Note"), self.note_var, 0, 2)

        if self._is_fuel:
            self._charger_reference_fuel()

        add_frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                                 border_width=1, border_color="#2a2d35")
        add_frame.pack(fill="x", padx=20, pady=(8, 4))
        add_inner = ctk.CTkFrame(add_frame, fg_color="transparent")
        add_inner.pack(fill="x", padx=12, pady=10)

        prod_col = ctk.CTkFrame(add_inner, fg_color="transparent")
        prod_col.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkLabel(prod_col, text=t("Produit"), font=ctk.CTkFont(size=10),
                     text_color="#9ca3af").pack(anchor="w")
        self.product_var = tk.StringVar()
        self.product_combo = ctk.CTkComboBox(prod_col, variable=self.product_var, values=[],
                                              command=self._on_product_change, height=30,
                                              corner_radius=6, fg_color="#22252b",
                                              border_color="#2a2d35")
        self.product_combo.pack(fill="x")
        self._all_product_display: list[str] = []
        self.product_var.trace_add("write", self._filter_product_list)
        # Saisie 100 % clavier : produit → (Tab ou Entrée) → quantité →
        # (Entrée) → ligne ajoutée → retour au produit. Aucune souris requise
        # pour enchaîner vingt lignes de réception.
        self.product_combo.bind("<Return>", self._scan_enter)
        self.product_combo.bind("<KP_Enter>", self._scan_enter)
        self.product_combo.bind("<Tab>", self._tab_produit)

        qty_col = ctk.CTkFrame(add_inner, fg_color="transparent", width=90)
        qty_col.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(qty_col, text=t("Quantité"), font=ctk.CTkFont(size=10),
                     text_color="#9ca3af").pack(anchor="w")
        self.qty_var = tk.StringVar()
        self.qty_entry = ctk.CTkEntry(qty_col, textvariable=self.qty_var, placeholder_text=t("ex: 10"),
                     width=90, height=30, corner_radius=6,
                     fg_color="#22252b", border_color="#2a2d35")
        self.qty_entry.pack()
        self.qty_entry.bind("<Return>", lambda e: self._add_line())
        # Pavé numérique : l'opérateur qui saisit des quantités à une main
        # valide avec l'Entrée du pavé, pas celle du clavier principal.
        self.qty_entry.bind("<KP_Enter>", lambda e: self._add_line())
        self.qty_entry.bind("<Tab>", self._tab_cycle)

        unit_col = ctk.CTkFrame(add_inner, fg_color="transparent", width=100)
        unit_col.pack(side="left", padx=(0, 8))
        ctk.CTkLabel(unit_col, text=t("Unité"), font=ctk.CTkFont(size=10),
                     text_color="#9ca3af").pack(anchor="w")

        self.unite_saisie_var = tk.StringVar(value="")
        self.unite_saisie_combo = ctk.CTkComboBox(
            unit_col, variable=self.unite_saisie_var, values=[""], width=100,
            height=30, corner_radius=6, state="readonly",
            fg_color="#22252b", border_color="#2a2d35")
        self.unite_saisie_combo.pack()
        self.unite_saisie_combo.pack_forget()

        self._unite_label = ctk.CTkLabel(unit_col, text="", text_color="#9ca3af",
                                         height=30)
        self._unite_label.pack()

        ctk.CTkButton(add_inner, text=t("+  Ajouter"), width=110, height=30,
                      corner_radius=6, fg_color="#2563eb", hover_color="#1d4ed8",
                      command=self._add_line).pack(side="left", anchor="s", pady=(14, 0))

        ctk.CTkButton(add_inner, text="↻", width=34, height=30,
                      corner_radius=6, fg_color="#2a2d35", hover_color="#353840",
                      command=self.refresh_products).pack(side="left", anchor="s",
                                                          pady=(14, 0), padx=(4, 0))

        if self._is_fuel_receiving:
            tank_info_frame = ctk.CTkFrame(self, fg_color="#1a2332", corner_radius=8,
                                             border_width=1, border_color="#1e3a5f")
            tank_info_frame.pack(fill="x", padx=20, pady=(2, 2))
            self._tank_info_var = tk.StringVar(value=t("Sélectionne une cuve pour voir son niveau"))
            self._tank_info_label = ctk.CTkLabel(
                tank_info_frame, textvariable=self._tank_info_var,
                font=ctk.CTkFont(size=12), text_color="#60a5fa",
                anchor="w")
            self._tank_info_label.pack(fill="x", padx=12, pady=6)

        table_frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                                   border_width=1, border_color="#2a2d35")
        table_frame.pack(fill="both", expand=True, padx=20, pady=(4, 4))

        columns = ("sku", "name", "quantity", "action")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", height=6)
        for col, text, width in (("sku", "SKU", 110), ("name", t("Produit"), 280),
                                  ("quantity", t("Quantité"), 120),
                                  ("action", "", 40)):
            self.tree.heading(col, text=text)
            self.tree.column(col, width=width, anchor="w")
        self.tree.column("action", anchor="center")
        self.tree.pack(side="left", fill="both", expand=True, padx=8, pady=8)
        # Double-clic = CORRIGER la quantité, plus la supprimer. Une faute de
        # frappe sur un bon de vingt lignes imposait sinon de retrouver le
        # produit dans la liste et de tout retaper. La suppression garde son
        # bouton dédié, en bas, et le clic droit ci-dessous.
        self.tree.bind("<Double-1>", lambda e: self._modifier_ligne_selectionnee())
        self.tree.bind("<Button-3>", self._menu_ligne)

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        # Pied du tableau : le total de ce qui est saisi, relu d'un coup d'œil
        # avant de valider. Directement sous la liste, pas mêlé aux boutons.
        pied_tableau = ctk.CTkFrame(self, fg_color="transparent")
        pied_tableau.pack(fill="x", padx=20, pady=(0, 2))
        self._line_count_label = ctk.CTkLabel(pied_tableau, text="",
                                              text_color="#9ca3af",
                                              font=ctk.CTkFont(size=12))
        self._line_count_label.pack(side="left")

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=20, pady=(0, 16))

        self.submit_btn = ctk.CTkButton(bottom, text=t(self.labels["verb"]),
                                        fg_color=self.labels["color"],
                                        hover_color=self.labels["hover"],
                                        height=36, corner_radius=8,
                                        font=ctk.CTkFont(size=13, weight="bold"),
                                        command=self._submit)
        self.submit_btn.pack(side="right")
        ctk.CTkButton(bottom, text=t("🗑  Retirer la ligne"), width=140,
                      fg_color="#2a2d35", hover_color="#353840",
                      height=32, corner_radius=6,
                      command=self._remove_selected_line).pack(side="right", padx=(0, 8))
        ctk.CTkButton(bottom, text=t("✏  Modifier la quantité"), width=170,
                      fg_color="#2a2d35", hover_color="#353840",
                      height=32, corner_radius=6,
                      command=self._modifier_ligne_selectionnee).pack(
                          side="right", padx=(0, 8))
        ctk.CTkButton(bottom, text=t("🔄  Nouveau bon vierge"), width=170,
                      fg_color="transparent", hover_color="#2a2d35",
                      border_width=1, border_color="#2a2d35",
                      text_color="#9ca3af", font=ctk.CTkFont(size=11),
                      height=32, corner_radius=6,
                      command=self._nouveau_bon_vierge).pack(side="right", padx=(0, 8))
        self.status_label = ctk.CTkLabel(bottom, text="", text_color="#f59e0b")
        self.status_label.pack(side="left", padx=(12, 0))

        self.dernier_bon_label = ctk.CTkLabel(self, text="", text_color="#6b7280",
                                               font=ctk.CTkFont(size=11))
        self.dernier_bon_label.pack(anchor="w", padx=20, pady=(0, 4))

        # Ctrl+Entree : la liaison est posee UNE SEULE FOIS dans main.py, qui
        # resout la vue affichee. Un bind_all ici serait ecrase par le dernier
        # formulaire construit (Reception/Expedition/Retour coexistent), et
        # Ctrl+Entree validerait alors le bon d'un autre onglet.

    @staticmethod
    def _field(parent, label, var, row, col, readonly=False):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=col, sticky="ew", padx=4, pady=3)
        ctk.CTkLabel(frame, text=label, font=ctk.CTkFont(size=10),
                     text_color="#9ca3af").pack(anchor="w")
        entry = ctk.CTkEntry(frame, textvariable=var, height=30, corner_radius=6,
                             fg_color="#22252b", border_color="#2a2d35")
        if readonly:
            entry.configure(state="disabled")
        entry.pack(fill="x")

    @staticmethod
    def _field_combo(parent, label, var, values, row, col, command=None):
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=col, sticky="ew", padx=4, pady=3)
        ctk.CTkLabel(frame, text=label, font=ctk.CTkFont(size=10),
                     text_color="#9ca3af").pack(anchor="w")
        combo = ctk.CTkComboBox(frame, variable=var, values=values, height=30,
                                corner_radius=6, state="readonly",
                                fg_color="#22252b", border_color="#2a2d35",
                                command=command)
        combo.pack(fill="x")
        return combo

    @staticmethod
    def _field_combo_editable(parent, label, var, values, row, col, command=None):
        """Comme `_field_combo`, mais éditable : sert à choisir une valeur
        connue (plaque, projet) SANS empêcher d'en saisir une nouvelle —
        « je peux ajouter » un véhicule ou un projet à la volée."""
        frame = ctk.CTkFrame(parent, fg_color="transparent")
        frame.grid(row=row, column=col, sticky="ew", padx=4, pady=3)
        ctk.CTkLabel(frame, text=label, font=ctk.CTkFont(size=10),
                     text_color="#9ca3af").pack(anchor="w")
        combo = ctk.CTkComboBox(frame, variable=var, values=values, height=30,
                                corner_radius=6,
                                fg_color="#22252b", border_color="#2a2d35",
                                command=command)
        combo.pack(fill="x")
        return combo

    def _charger_reference_fuel(self):
        """Peuple les combos Plaque (autocomplétion) et Projet (suggestions)."""
        def _ok_plaques(data):
            if self.winfo_exists():
                self.vehicle_combo.configure(values=data.get("plates", []))
        run_async(self, self.api.get_fuel_vehicles, _ok_plaques, lambda _e: None)

        if self.project_combo is not None:
            def _ok_projets(data):
                if self.winfo_exists():
                    self.project_combo.configure(values=data.get("projects", []))
            run_async(self, self.api.get_reference_fuel, _ok_projets, lambda _e: None)

    def _on_region_change(self, region: str):
        """Le superviseur se remplit tout seul à partir de la région.

        Le premier de la liste est le superviseur principal de la région ; il
        reste modifiable pour les régions qui en comptent plusieurs.

        Exception : une livraison adressée à un technicien nommé. Les
        techniciens ne dépendent d'aucun superviseur régional ; leur coller
        automatiquement le superviseur de la région invente un rattachement
        qui n'existe pas et fausse l'attribution du stock terrain.
        """
        if self.doc_type not in ("DELIVERY", "RETURN") or self._is_fuel:
            return
        supervisors = supervisors_for(region)
        self.operator_combo.configure(values=supervisors)
        if self._technicien():
            # Le champ reste libre : à l'opérateur de le remplir s'il y a
            # vraiment un superviseur impliqué.
            if self.operator_var.get() not in supervisors:
                self.operator_var.set("")
            return
        if supervisors:
            if self.operator_var.get() not in supervisors:
                self.operator_var.set(supervisors[0])
        else:
            self.operator_var.set("")

    def _on_technicien_change(self, _valeur=None):
        """Le choix d'un technicien retire le superviseur auto-rempli.

        L'ordre de saisie ne doit pas changer le résultat : si la région a
        déjà posé un superviseur avant que le technicien soit choisi, ce nom
        est effacé. Revenir à « Aucun » remet le comportement régional.
        """
        if self.doc_type != "DELIVERY" or self._is_fuel:
            return
        region = self.region_var.get().strip()
        if self._technicien():
            supervisors = supervisors_for(region)
            # On n'efface que le nom posé automatiquement : un superviseur
            # saisi à la main hors de la liste régionale est respecté.
            if self.operator_var.get() in supervisors:
                self.operator_var.set("")
        elif region:
            self._on_region_change(region)

    def _on_supervisor_change(self, supervisor: str):
        """Complète la région si le superviseur n'en a qu'une seule.

        Le choix reste volontairement manuel pour les superviseurs intervenant
        dans plusieurs régions : compléter au hasard déplacerait le stock vers
        un mauvais emplacement terrain.
        """
        if self.doc_type not in ("DELIVERY", "RETURN"):
            return
        possibles = regions_for_supervisor(supervisor)
        if len(possibles) == 1:
            region = possibles[0]
            self.region_var.set(region)
            self._on_region_change(region)
            self.status_label.configure(text="")
        elif len(possibles) > 1:
            self.status_label.configure(
                text=t("Ce superviseur est rattaché à plusieurs régions : "
                       "sélectionne la région.")
            )

    def refresh_products(self, apres=None):
        """Recharge le catalogue (et les fournisseurs) sans figer l'interface.

        `apres` est rappelé sur la boucle Tk une fois le catalogue en place :
        indispensable pour tout ce qui a besoin de `self.products` (la
        duplication d'un bon, par exemple), le chargement n'étant plus
        terminé au retour de cette méthode.

        Appelée après chaque bon enregistré, à la duplication depuis
        l'historique et à chaque changement d'onglet : exécutée sur la boucle
        Tk, elle gelait la fenêtre le temps du réseau (jusqu'au timeout).
        """
        besoin_contacts = (self.doc_type == "RECEIVING"
                           and self.source_combo is not None)

        def _charger():
            hors_ligne = False
            try:
                produits = self.api.get_products()
                sauvegarder_produits(produits, sector=self.sector)
            except ApiError:
                # Serveur injoignable : on repart du dernier catalogue connu,
                # sinon l'opérateur ne peut plus rien saisir alors que la file
                # d'attente existe justement pour travailler hors ligne. Le
                # cache est étiqueté par secteur (local_cache.py) : un poste
                # qui a servi un autre secteur en dernier ne propose rien
                # plutôt que le mauvais catalogue.
                produits = charger_produits(sector=self.sector)
                hors_ligne = True
            contacts = None
            if besoin_contacts:
                if not hors_ligne:
                    try:
                        contacts = self.api.get_contacts("supplier")
                        sauvegarder_contacts("supplier", contacts, sector=self.sector)
                    except ApiError:
                        contacts = None
                if contacts is None:
                    contacts = charger_contacts("supplier", sector=self.sector)
            return produits, hors_ligne, contacts

        def _termine():
            if apres is not None and self.winfo_exists():
                apres()

        def _ok(resultat):
            self._appliquer_catalogue(resultat)
            _termine()

        def _err(exc):
            self._erreur_catalogue(exc)
            _termine()

        run_async(self, _charger, _ok, _err)

    def _erreur_catalogue(self, exc):
        if self.winfo_exists():
            self.status_label.configure(text=str(exc))

    def _appliquer_catalogue(self, resultat):
        """Applique le catalogue chargé — exécuté sur la boucle Tk."""
        if not self.winfo_exists():
            return
        produits, hors_ligne, contacts = resultat
        if hors_ligne and not produits:
            self.status_label.configure(
                text=t("Serveur injoignable et aucun catalogue en mémoire — "
                       "impossible de saisir pour l'instant"))
            return
        self.products = produits
        self.status_label.configure(
            text=t("Mode hors ligne — catalogue en cache") if hors_ligne else "")
        display = [
            t("{sku} - {nom} (stock: {stock:g} {unite})").format(
                sku=p["sku"], nom=p["name"], stock=p["current_stock"],
                unite=p.get("unit", "pcs"))
            for p in self.products
        ]
        self._all_product_display = display
        self.product_combo.configure(values=display)
        if contacts:
            noms = [c["name"] for c in contacts if c.get("active", True)]
            if self._is_fuel_receiving:
                base = (["Gulf Oil", "National Petroleum", "Supplier"]
                        if self.demo_mode else ["Supplier"])
            else:
                base = (["Central Supply", "Supplier"] if self.demo_mode
                        else ["Digicel WH", "Supplier"])
            for n in noms:
                if n not in base:
                    base.append(n)
            self._sources_defaut = base
            self.source_combo.configure(values=self._sources_defaut)
            # Résout la source choisie vers son id de contact : rattache le
            # bon à la fiche (historique fournisseur) sans changer la saisie,
            # qui reste du texte libre pour un fournisseur non enregistré.
            self._contacts_par_nom = {
                c["name"]: c["id"] for c in contacts if c.get("active", True)
            }
        # Le résumé du pied dépend du catalogue (unité, coût unitaire) : un bon
        # repris avant la fin du chargement affichait sinon des « pcs » et pas
        # de valeur jusqu'à la ligne suivante.
        self._update_line_count()

    def _filter_product_list(self, *_args):
        typed = normaliser(self.product_var.get())
        if not self._all_product_display:
            return
        if not typed:
            self.product_combo.configure(values=self._all_product_display)
            return
        # Tolérante aux fautes sur le NOM (voir recherche.py). Le SKU, lui,
        # reste littéral : le premier segment du libellé est la référence, et
        # une référence approchée est une erreur de saisie, pas une aide.
        filtered = [d for d in self._all_product_display
                    if correspond_libelle(typed, d)]
        if filtered:
            self.product_combo.configure(values=filtered)

    def _scan_enter(self, event=None):
        typed = self.product_var.get().strip()
        if not typed:
            # Champ produit vide : Entrée saute à la quantité plutôt que de ne
            # rien faire — l'opérateur qui a choisi son produit à la souris
            # enchaîne au clavier.
            self.qty_entry.focus_set()
            return "break"
        # Le SKU exact prime sur la recherche partielle : scanner « AF1 »
        # ne doit jamais sélectionner « AF10 ».
        exact = chercher_par_sku(self.products, typed.lower())
        if exact is not None:
            prefixe = f"{exact['sku']} - "
            for d in self._all_product_display:
                if d.startswith(prefixe):
                    self._choisir_produit(d)
                    return "break"
        # Correspondance littérale ensuite, tolérante en dernier recours : on
        # ne veut jamais qu'un produit approché passe devant un produit
        # réellement contenu dans la saisie.
        normalisee = normaliser(typed)
        for d in self._all_product_display:
            if normalisee in normaliser(d):
                self._choisir_produit(d)
                return "break"
        for d in self._all_product_display:
            if correspond_libelle(normalisee, d):
                self._choisir_produit(d)
                return "break"
        self.product_combo.configure(values=self._all_product_display)
        self.status_label.configure(text=t("Aucun produit ne correspond à « {saisie} »").format(saisie=typed))
        return "break"

    def _choisir_produit(self, libelle: str):
        """Pose le produit dans le champ et enchaîne sur la quantité.

        La quantité est présélectionnée : un reste de saisie précédente est
        écrasé par la première touche, sans effacement manuel.
        """
        self.product_var.set(libelle)
        self._on_product_change(libelle)
        self._focus_quantite()

    def _focus_quantite(self, event=None):
        self.qty_entry.focus_set()
        try:
            self.qty_entry.select_range(0, "end")
        except Exception:
            # `select_range` n'existe que sur l'Entry interne du CTkEntry
            # selon les versions : le focus est l'essentiel, pas la sélection.
            pass
        return "break"

    def _tab_produit(self, event=None):
        """Tab depuis le produit descend sur la quantité.

        Sans ce lien explicite, Tab suivait l'ordre interne du ComboBox et
        atterrissait sur la flèche du menu déroulant : l'opérateur croyait
        être dans la quantité et tapait dans le vide.
        """
        return self._focus_quantite()

    def _tab_cycle(self, event=None):
        """Apres la quantite, Tab ramene le focus sur le produit."""
        self.product_combo.focus_set()
        return "break"

    def _on_product_change(self, _value: str):
        product = self._selected_product()
        if product and product.get("unit_type") == "volume" and product.get("bidon_capacity"):
            cap = product["bidon_capacity"]
            self.unite_saisie_combo.configure(
                values=["gls", f"{self._prefixe_bidons} ({cap:g} gls)"])
            self.unite_saisie_var.set("gls")
            self._unite_label.pack_forget()
            self.unite_saisie_combo.pack()
        elif product and product.get("unit_type") == "volume":
            self.unite_saisie_combo.pack_forget()
            self._unite_label.configure(text="gls")
            self._unite_label.pack()
        else:
            self.unite_saisie_combo.pack_forget()
            self._unite_label.configure(text="pcs")
            self._unite_label.pack()

        if self._is_fuel_receiving and product:
            cap = product.get("tank_capacity", 0)
            stock = product.get("current_stock", 0)
            dispo = max(0, cap - stock) if cap else 0
            if cap:
                pct = (stock / cap * 100) if cap else 0
                info = t("⛽ Cuve : {nom}  |  Niveau : {stock:,.0f}/{cap:,.0f} gls ({pct:.0f}%)  |  Disponible : {dispo:,.0f} gls").format(
                    nom=product.get("name", ""), stock=stock, cap=cap, pct=pct, dispo=dispo)
            else:
                info = t("⛽ Cuve : {nom}  |  Niveau : {stock:,.0f} gls").format(
                    nom=product.get("name", ""), stock=stock)
            self._tank_info_var.set(info)
            if cap and dispo < cap * 0.1:
                self._tank_info_label.configure(text_color="#ef4444")  # Rouge si presque pleine
            elif cap and dispo < cap * 0.25:
                self._tank_info_label.configure(text_color="#f59e0b")  # Orange si remplissage > 75%
            else:
                self._tank_info_label.configure(text_color="#60a5fa")  # Bleu normal

    def _selected_product(self) -> dict | None:
        value = self.product_var.get()
        for p in self.products:
            if value.startswith(f"{p['sku']} - "):
                return p
        return None

    # ------------------------------------------------------------- brouillon

    def _sauvegarder_brouillon(self):
        """Mémorise le bon en cours après chaque ligne ajoutée.

        Une coupure de courant en pleine saisie d'un bon de vingt lignes ne
        doit pas faire tout recommencer.
        """
        if not self.lines:
            effacer_brouillon(self.doc_type)
            return
        lignes = []
        for cle in self.lines.cles():
            valeurs = self.tree.item(cle, "values")
            ligne = dict(self.lines._lignes[cle])
            ligne["affichage"] = valeurs[2] if len(valeurs) > 2 else ""
            lignes.append(ligne)
        brouillon = {
            "party": self.party_var.get(),
            "region": self.region_var.get(),
            "operator": self.operator_var.get(),
            "reference": self.reference_var.get(),
            "note": self.note_var.get(),
            "carrier": self.transporteur_var.get(),
            "technician": self._technicien(),
            "lines": lignes,
        }
        if self._is_fuel:
            brouillon["vehicle_plate"] = self.vehicle_plate_var.get()
            brouillon["project"] = self.project_var.get()
            brouillon["mileage"] = self.mileage_var.get()
            brouillon["fuel_card"] = self.fuel_card_var.get()
        sauvegarder_brouillon(self.doc_type, brouillon, sector=self.sector)

    def _proposer_reprise_brouillon(self):
        if self._brouillon_propose:
            return
        self._brouillon_propose = True
        if self.lines:
            return
        brouillon = charger_brouillon(self.doc_type, sector=self.sector)
        if not brouillon:
            return
        nb = len(brouillon.get("lines", []))
        if not messagebox.askyesno(
            t("Bon en cours retrouvé"),
            t("Bon non terminé {origine} — {nb} ligne(s).\n\n"
              "Le reprendre ?").format(
                  origine=self._origine_brouillon(brouillon), nb=nb),
        ):
            effacer_brouillon(self.doc_type)
            return
        self._restaurer_brouillon(brouillon)

    @staticmethod
    def _origine_brouillon(brouillon: dict) -> str:
        """« du 12/03 à 14h05 par Idens » — le poste est partagé, et un bon
        retrouvé sans date ni auteur ne dit pas s'il vient de la saisie
        interrompue il y a cinq minutes ou de celle d'un collègue."""
        morceaux = []
        horodatage = brouillon.get("created_at")
        if horodatage:
            try:
                quand = datetime.fromisoformat(horodatage)
                morceaux.append(t("du {quand}").format(
                    quand=quand.strftime("%d/%m à %Hh%M")))
            except (TypeError, ValueError):
                pass
        auteur = (brouillon.get("username") or "").strip()
        if auteur:
            morceaux.append(t("par {auteur}").format(auteur=auteur))
        return " ".join(morceaux) if morceaux else t("retrouvé sur ce poste")

    def _restaurer_brouillon(self, brouillon: dict):
        self.party_var.set(brouillon.get("party") or "")
        if self.doc_type != "RECEIVING":
            self.region_var.set(brouillon.get("region") or "")
        self.operator_var.set(brouillon.get("operator") or "")
        self.reference_var.set(brouillon.get("reference") or "")
        self.note_var.set(brouillon.get("note") or "")
        self.transporteur_var.set(brouillon.get("carrier") or "")
        self._set_technicien(brouillon.get("technician"))
        if self._is_fuel:
            self.vehicle_plate_var.set(brouillon.get("vehicle_plate") or "")
            self.project_var.set(brouillon.get("project") or "")
            self.mileage_var.set(brouillon.get("mileage") or "")
            self.fuel_card_var.set(brouillon.get("fuel_card") or "")
        for ligne in brouillon.get("lines", []):
            try:
                item_id = self.tree.insert(
                    "", "end",
                    values=(ligne["sku"], ligne["name"],
                            ligne.get("affichage") or f"{ligne['quantity']:g}", "✕"))
                self.lines.ajouter(item_id, ligne["product_id"], ligne["sku"],
                                   ligne["name"], ligne["quantity"])
            except (KeyError, TypeError):
                continue
        self._update_line_count()
        self.status_label.configure(
            text=t("Bon repris — vérifie les quantités avant d'enregistrer"))

    def _add_line(self):
        product = self._selected_product()
        if not product:
            self.status_label.configure(text=t("Sélectionne un produit valide"))
            # Le focus retourne sur le champ fautif : sans ça, l'opérateur au
            # clavier restait dans la quantité et réappuyait sur Entrée en
            # boucle sans comprendre ce qui bloquait.
            self.product_combo.focus_set()
            return
        try:
            qty = float_saisie(self.qty_var.get())
            if qty <= 0:
                raise ValueError
        except ValueError:
            self.status_label.configure(text=t("Quantité invalide"))
            self._focus_quantite()
            return

        unite = product.get("unit", "pcs")
        saisie_bidon = False
        if product.get("unit_type") == "volume" and product.get("bidon_capacity"):
            choix = self.unite_saisie_var.get()
            if choix.startswith(self._prefixe_bidons):
                qty_affichee = qty
                qty = round(qty * product["bidon_capacity"], 3)
                saisie_bidon = True

        deja = self.lines.quantite_totale(product["id"])
        if self.doc_type in ("DELIVERY",) and deja + qty > product["current_stock"]:
            self.status_label.configure(
                text=t("Stock insuffisant : {demande:g} {unite} demandés au "
                       "total sur ce bon, {dispo:g} {unite} disponibles").format(
                           demande=deja + qty, unite=unite,
                           dispo=product["current_stock"])
            )
            return

        # Un produit déjà saisi ne crée pas une deuxième ligne : les quantités
        # sont additionnées sur la ligne existante. Deux lignes pour le même
        # produit sont une source d'erreur de comptage à la relecture du bon.
        cle_existante = self.lines.cle_pour_produit(product["id"])
        if cle_existante is not None:
            total = round(self.lines.quantite(cle_existante) + qty, 3)
            self.lines.definir_quantite(cle_existante, total)
            self.tree.item(cle_existante,
                           values=(product["sku"], product["name"],
                                   f"{total:g} {unite}", "✕"))
            message = t("Quantité fusionnée avec la ligne existante")
        else:
            if saisie_bidon:
                affichage = t("{qty:g} {unite} ({bidons:g} bid.)").format(
                    qty=qty, unite=unite, bidons=qty_affichee)
            else:
                affichage = f"{qty:g} {unite}"
            item_id = self.tree.insert(
                "", "end", values=(product["sku"], product["name"], affichage, "✕"))
            self.lines.ajouter(item_id, product["id"], product["sku"], product["name"], qty)
            message = ""

        self.qty_var.set("")
        self.product_var.set("")
        self.product_combo.configure(values=self._all_product_display)
        self.status_label.configure(text=message)
        self._update_line_count()
        self._sauvegarder_brouillon()
        self.product_combo.focus_set()

    def _remove_selected_line(self):
        for item_id in self.tree.selection():
            self.lines.retirer(item_id)
            self.tree.delete(item_id)
        self._update_line_count()
        self._sauvegarder_brouillon()

    # --------------------------------------------- modification d'une ligne

    def _ligne_selectionnee(self) -> str | None:
        """Clé de la ligne sélectionnée, ou None (avec le message qui va bien)."""
        selection = self.tree.selection()
        if not selection:
            self.status_label.configure(
                text=t("Clique d'abord sur la ligne à modifier"))
            return None
        return selection[0]

    def _modifier_ligne_selectionnee(self, item_id: str | None = None):
        """Corrige la quantité d'une ligne DÉJÀ saisie, sans la retirer."""
        if item_id is None:
            item_id = self._ligne_selectionnee()
            if item_id is None:
                return
        ligne = self.lines.ligne(item_id)
        if ligne is None:
            return
        unite = self._unite_produit(ligne["product_id"])
        from views.dialogues import demander_quantite
        texte = demander_quantite(
            self.winfo_toplevel(),
            t("Modifier la quantité"),
            t("{sku} — {nom}\n\nNouvelle quantité :").format(
                sku=ligne["sku"], nom=ligne["name"]),
            valeur_initiale=f"{ligne['quantity']:g}",
            unite=unite,
        )
        if texte is None:
            return
        self._appliquer_modification_ligne(item_id, texte)

    def _unite_produit(self, product_id) -> str:
        """Unité de base du produit — « pcs » par défaut.

        Le catalogue peut ne pas être encore chargé (brouillon repris juste
        après l'ouverture de l'écran) : mieux vaut l'unité par défaut, la même
        que partout ailleurs, qu'une erreur.
        """
        for p in self.products:
            if p["id"] == product_id:
                return p.get("unit", "pcs")
        return "pcs"

    def _appliquer_modification_ligne(self, item_id: str, texte) -> bool:
        """Valide la saisie et remplace la quantité de la ligne. True si appliqué.

        Séparée du dialogue pour être vérifiable sans ouvrir de fenêtre.
        """
        ligne = self.lines.ligne(item_id)
        if ligne is None:
            return False
        try:
            quantite = float_saisie(texte)
            if quantite <= 0:
                raise ValueError
        except (ValueError, TypeError):
            self.status_label.configure(
                text=t("Quantité invalide — la ligne n'a pas été modifiée"))
            return False

        unite = self._unite_produit(ligne["product_id"])
        # Le plafond de stock ne vaut que pour une SORTIE, et la quantité que
        # l'on remplace ne doit pas se compter deux fois (corriger 40 en 30
        # sur un stock de 35 est légitime).
        if self.doc_type == "DELIVERY":
            produit = None
            for p in self.products:
                if p["id"] == ligne["product_id"]:
                    produit = p
                    break
            if produit is not None:
                autres = self.lines.quantite_totale_hors(
                    ligne["product_id"], item_id)
                if autres + quantite > produit["current_stock"]:
                    self.status_label.configure(
                        text=t("Stock insuffisant : {demande:g} {unite} demandés au "
                               "total sur ce bon, {dispo:g} {unite} disponibles").format(
                                   demande=autres + quantite, unite=unite,
                                   dispo=produit["current_stock"]))
                    return False

        quantite = round(quantite, 3)
        self.lines.definir_quantite(item_id, quantite)
        self.tree.item(item_id, values=(ligne["sku"], ligne["name"],
                                        f"{quantite:g} {unite}", "✕"))
        self.status_label.configure(
            text=t("Ligne modifiée : {sku} → {qty:g} {unite}").format(
                sku=ligne["sku"], qty=quantite, unite=unite))
        self._update_line_count()
        self._sauvegarder_brouillon()
        return True

    def _menu_ligne(self, event):
        """Clic droit sur une ligne : modifier ou retirer.

        Le clic droit ne sélectionne pas tout seul dans un Treeview : sans
        cette ligne, le menu aurait porté sur la sélection précédente.
        """
        item_id = self.tree.identify_row(event.y)
        if not item_id:
            return
        self.tree.selection_set(item_id)
        menu = tk.Menu(self.tree, tearoff=0)
        menu.add_command(label=t("Modifier la quantité"),
                         command=lambda: self._modifier_ligne_selectionnee(item_id))
        menu.add_command(label=t("Retirer la ligne"),
                         command=self._remove_selected_line)
        try:
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            menu.grab_release()

    def _update_line_count(self):
        """Pied du tableau : lignes, totaux PAR UNITÉ, et valeur si connue."""
        produits_par_id = {p["id"]: p for p in self.products}
        resume = resumer_lignes(self.lines.toutes(), produits_par_id)
        self._line_count_label.configure(text=texte_resume(resume, t))

    def _show_toast(self, message: str, color: str = SUCCESS, duration: int = 4000):
        """Affiche un message temporaire dans le label dernier_bon_label."""
        if not self.winfo_exists():
            return
        # Un toast chasse le precedent : sans annulation, l'extinction du
        # premier eteignait le second a peine affiche (rafale WebSocket).
        if self._toast_after_id is not None:
            try:
                self.after_cancel(self._toast_after_id)
            except Exception:
                pass
        self.dernier_bon_label.configure(text=message, text_color=color)
        # Disparition progressive apres `duration` ms
        self._toast_after_id = self.after(duration, self._fade_toast)

    def _fade_toast(self):
        """Estompe le toast en passant a une couleur muette."""
        self._toast_after_id = None
        # La vue est detruite a la deconnexion : un `after` deja programme
        # arriverait quand meme et leverait une TclError dans la boucle Tk.
        if not self.winfo_exists():
            return
        self.dernier_bon_label.configure(text_color="#6b7280")

    def _clear_form(self, demander_confirmation: bool = True) -> bool:
        """Vide le formulaire : champs d'en-tete, lignes, statut.

        Demande confirmation si des lignes sont deja saisies : Escape ne doit
        jamais faire disparaitre un bon en cours sans avertissement.
        Renvoie True si le formulaire a bien ete vide.
        """
        if demander_confirmation and len(self.lines):
            n = len(self.lines)
            if not messagebox.askyesno(
                t("Vider le formulaire"),
                t("Un bon est en cours avec {n} ligne(s).\n\n"
                  "Tout effacer ?").format(n=n),
            ):
                return False
        self.party_var.set("")
        self.region_var.set("")
        self.reference_var.set("")
        if self._is_fuel:
            self.vehicle_plate_var.set("")
            self.project_var.set("")
            self.mileage_var.set("")
            self.fuel_card_var.set("")
        elif self.doc_type in ("DELIVERY", "RETURN"):
            # Le superviseur depend de la region : la vider sans reinitialiser
            # le combo laisserait un nom et une liste filtree incoherents.
            self._on_region_change("")
            self.operator_var.set("")
        self.note_var.set("")
        self.transporteur_var.set("")
        self._set_technicien("")
        self.date_var.set(datetime.now().strftime("%Y-%m-%d %H:%M"))
        self.product_var.set("")
        self.qty_var.set("")
        self.lines.vider()
        self.tree.delete(*self.tree.get_children())
        self.status_label.configure(text="")
        self._update_line_count()
        # La clé d'idempotence n'a de sens que tant que le bon en cours est le
        # MÊME. Elle est volontairement conservée après un envoi en échec (un
        # nouvel essai ne doit pas créer de doublon si le premier était en fait
        # passé côté serveur) — mais ici le bon disparaît. La garder aurait fait
        # rattacher le bon SUIVANT, entièrement différent, à celui d'avant : le
        # serveur aurait répondu « déjà enregistré » et la nouvelle saisie
        # aurait été perdue sans que rien ne le signale.
        self._cle_idempotence = None
        effacer_brouillon(self.doc_type)
        # En reception, la destination est toujours l'entrepot : le champ ne
        # doit jamais rester vide apres un effacement.
        if self.doc_type == "RECEIVING":
            self.region_var.set("TP-WH")
        self.product_combo.focus_set()
        return True

    def _nouveau_bon_vierge(self):
        """Remet tout le formulaire à zéro, en-tête compris."""
        if self.lines and not messagebox.askyesno(
            t("Nouveau bon vierge"),
            t("Le bon en cours contient des lignes non enregistrées.\n\n"
              "Tout effacer et repartir d'un bon vide ?"),
        ):
            return
        self._cle_idempotence = None
        # La confirmation vient d'etre donnee ci-dessus : pas de double question.
        self._clear_form(demander_confirmation=False)
        if self.doc_type == "RECEIVING":
            self.region_var.set("TP-WH")
        else:
            self.operator_var.set("")

    def _add_line_from_data(self, product_id: int, quantity: float):
        """Ajoute une ligne au treeview a partir d'un product_id et d'une quantite.

        Utilisee par pre_remplir() pour la duplication de bon.
        """
        product = None
        for p in self.products:
            if p["id"] == product_id:
                product = p
                break
        if not product:
            return
        unite = product.get("unit", "pcs")
        affichage = f"{quantity:g} {unite}"
        item_id = self.tree.insert(
            "", "end", values=(product["sku"], product["name"], affichage, "✕"))
        self.lines.ajouter(item_id, product["id"], product["sku"], product["name"], quantity)
        self._update_line_count()

    def pre_remplir(self, bon_data: dict):
        """Pre-remplit le formulaire avec les donnees d'un bon duplique.

        Ne set PAS la reference (c'est un nouveau bon).
        """
        # Les lignes ne peuvent être résolues (product_id -> SKU/nom/unité)
        # qu'une fois le catalogue chargé : on les ajoute dans le callback.
        def _remplir_lignes():
            for line in bon_data.get("lines", []):
                self._add_line_from_data(line["product_id"], line["quantity"])

        self.refresh_products(apres=_remplir_lignes)
        self.party_var.set(bon_data.get("party") or "")
        region = bon_data.get("region") or ""
        # En reception la destination est toujours l'entrepot et le champ est
        # en lecture seule : dupliquer un bon ne doit jamais le laisser vide.
        if self.doc_type == "RECEIVING":
            region = "TP-WH"
        self.region_var.set(region)
        # Recharge la liste des superviseurs de la region AVANT de reposer le
        # superviseur du bon duplique : sinon le ComboBox garde la liste
        # precedente et peut afficher une valeur absente des options.
        self._on_region_change(region)
        self.operator_var.set(bon_data.get("operator") or "")
        self.note_var.set("")
        self.transporteur_var.set(bon_data.get("carrier") or "")
        self._set_technicien(bon_data.get("technician"))
        if self._is_fuel:
            self.vehicle_plate_var.set(bon_data.get("vehicle_plate") or "")
            self.project_var.set(bon_data.get("project") or "")
            self.mileage_var.set(
                f"{bon_data['mileage']:g}" if bon_data.get("mileage") is not None else "")
            self.fuel_card_var.set(bon_data.get("fuel_card") or "")

    def _technicien(self) -> str:
        """Technicien choisi, ou chaîne vide pour « Aucun »."""
        valeur = self.technicien_var.get().strip()
        return "" if valeur == self._aucun_technicien else valeur

    def _set_technicien(self, valeur) -> None:
        if self.doc_type != "DELIVERY":
            return
        self.technicien_var.set(valeur or self._aucun_technicien)

    def _saisisseur(self) -> str:
        """Opérateur qui saisit, lu de la configuration du poste.

        Distinct du champ Superviseur, qui désigne le destinataire terrain.
        """
        try:
            from config import load_config
            return load_config().get("operator", "")
        except Exception:
            return ""

    def actualiser_date(self):
        if not self.lines:
            self.date_var.set(datetime.now().strftime("%Y-%m-%d %H:%M"))

    def mettre_a_jour_operateur_defaut(self, ancien: str, nouveau: str):
        """Actualise une réception ouverte, pas le superviseur d'une sortie."""
        if self.doc_type == "RECEIVING":
            self.operator_var.set(
                remplacer_operateur_defaut(self.operator_var.get(), ancien, nouveau)
            )

    def _notifier_file(self):
        if self._on_queue_change:
            self._on_queue_change()

    def _parse_date(self) -> str | None:
        """Valide la date du bon.

        Renvoie la date normalisée, None si le champ est vide, ou un marqueur
        ``__invalid__`` / ``__futur__`` / ``__refuse__`` que ``_submit``
        traduit en message pour l'opérateur.
        """
        raw = self.date_var.get().strip()
        if not raw:
            return None
        parsee = None
        for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
            try:
                parsee = datetime.strptime(raw, fmt)
                break
            except ValueError:
                continue
        if parsee is None:
            return "__invalid__"

        maintenant = datetime.now()
        # Tolérance d'une minute : l'heure du poste peut légèrement dériver.
        if parsee > maintenant + timedelta(minutes=1):
            return "__futur__"

        if maintenant - parsee > timedelta(days=7):
            if not messagebox.askyesno(
                t("Date ancienne"),
                t("La date saisie est le {date}.\n\nCette date est ancienne "
                  "(plus de 7 jours) — confirmer ?").format(
                      date=parsee.strftime("%d/%m/%Y")),
            ):
                return "__refuse__"

        return parsee.strftime("%Y-%m-%d %H:%M:%S")

    def _submit(self):
        if str(self.submit_btn.cget("state")) == "disabled":
            return
        if not self.lines:
            self.status_label.configure(
                text=t("Ajoute au moins une ligne avant de valider"))
            return

        if self._is_fuel_receiving:
            nb = len(self.lines)
            total_qty = sum(l["quantity"] for l in self.lines.payload())
            noms_cuves = []
            for l in self.lines.payload():
                for p in self.products:
                    if p["id"] == l["product_id"]:
                        noms_cuves.append(p["name"])
                        break
            msg = t("Réception de carburant :\n\n"
                    "  {nb} cuve(s), {total:,.0f} gallons au total\n"
                    "  Fournisseur : {fournisseur}\n"
                    "  Camion : {plaque}\n"
                    "  N° Bordereau : {ref}\n"
                    "  Cuves : {cuves}\n\n"
                    "Confirmer ?").format(
                        nb=nb, total=total_qty,
                        fournisseur=self.party_var.get().strip() or "—",
                        plaque=self.vehicle_plate_var.get().strip() or "—",
                        ref=self.reference_var.get().strip() or "—",
                        cuves=", ".join(noms_cuves) if noms_cuves else "—")
            if not messagebox.askyesno(t("Confirmer la réception de carburant"), msg):
                return
        elif self.doc_type == "RECEIVING":
            nb = len(self.lines)
            total_qty = sum(l["quantity"] for l in self.lines.payload())
            msg = t("Réception de {nb} produit(s) — {total:g} unité(s) au "
                    "total.\n\nConfirmer l'enregistrement ?").format(
                        nb=nb, total=total_qty)
            if not messagebox.askyesno(t("Confirmer la réception"), msg):
                return

        if self._is_fuel and not self._is_fuel_receiving:
            if self.doc_type == "DELIVERY" and not self.vehicle_plate_var.get().strip():
                self.status_label.configure(
                    text=t("La plaque du véhicule est obligatoire pour une "
                           "livraison de carburant"))
                return
            mileage_raw = self.mileage_var.get().strip()
            if mileage_raw:
                try:
                    if float_saisie(mileage_raw) < 0:
                        raise ValueError
                except ValueError:
                    self.status_label.configure(text=t("Kilométrage invalide"))
                    return
        elif self.doc_type in ("DELIVERY", "RETURN") and not self.region_var.get().strip():
            self.status_label.configure(
                text=t("La région est obligatoire pour {quoi} (elle rattache "
                       "le mouvement à un superviseur)").format(
                           quoi=t("une expédition")
                           if self.doc_type == "DELIVERY" else t("un retour"))
            )
            return

        created_at = self._parse_date()
        if created_at == "__invalid__":
            self.status_label.configure(
                text=t("Date invalide (format attendu : AAAA-MM-JJ HH:MM)"))
            return
        if created_at == "__futur__":
            self.status_label.configure(
                text=t("La date ne peut pas être dans le futur"))
            return
        if created_at == "__refuse__":
            self.status_label.configure(
                text=t("Corrige la date du bon avant de valider"))
            return

        if self._is_fuel:
            nb = len(self.lines)
            total_qty = sum(l["quantity"] for l in self.lines.payload())
            titre = (t("Confirmer la livraison de carburant") if self.doc_type == "DELIVERY"
                     else t("Confirmer le retour"))
            msg = t("Tu vas enregistrer :\n\n"
                    "  {nb} ligne(s), {total:g} unité(s) au total\n"
                    "  Véhicule : {plaque}\n"
                    "  Projet : {projet}\n"
                    "  Receveur : {receveur}\n\n"
                    "Confirmer ?").format(
                        nb=nb, total=total_qty,
                        plaque=self.vehicle_plate_var.get().strip() or "—",
                        projet=self.project_var.get().strip() or "—",
                        receveur=self.party_var.get().strip() or "—")
            if not messagebox.askyesno(titre, msg):
                return
        elif self.doc_type in ("DELIVERY", "RETURN"):
            nb = len(self.lines)
            total_qty = sum(l["quantity"] for l in self.lines.payload())
            region = self.region_var.get().strip() or "—"
            if self.doc_type == "DELIVERY":
                titre = t("Confirmer l'expédition")
                msg = t("Tu vas enregistrer une expédition :\n\n"
                        "  {nb} ligne(s), {total:g} unité(s) au total\n"
                        "  Région : {region}\n"
                        "  Réceptionnaire : {party}\n"
                        "  Technicien : {technicien}\n\n"
                        "Confirmer ?").format(
                            nb=nb, total=total_qty, region=region,
                            party=self.party_var.get().strip() or "—",
                            technicien=self._technicien() or "—")
            else:
                titre = t("Confirmer le retour")
                msg = t("Tu vas enregistrer un retour :\n\n"
                        "  {nb} ligne(s), {total:g} unité(s) au total\n"
                        "  Région d'origine : {region}\n"
                        "  Provenance : {party}\n\n"
                        "Les produits seront remis en stock entrepôt.\n"
                        "Confirmer ?").format(
                            nb=nb, total=total_qty, region=region,
                            party=self.party_var.get().strip() or "—")
            if not messagebox.askyesno(titre, msg):
                return

        if self._cle_idempotence is None:
            self._cle_idempotence = str(uuid.uuid4())

        self.submit_btn.configure(state="disabled", text=t("Envoi en cours..."))
        note = self.note_var.get().strip()
        transporteur = self.transporteur_var.get().strip()

        region = self.region_var.get().strip()
        if self.doc_type == "RECEIVING":
            region = ""
        party = self.party_var.get().strip()
        payload_envoi = {
            "doc_type": self.doc_type,
            "party": party,
            "party_contact_id": self._contacts_par_nom.get(party),
            "operator": self.operator_var.get().strip(),
            "region": region,
            "reference": self.reference_var.get().strip(),
            "note": note,
            "carrier": transporteur or None,
            "lines": self.lines.payload(),
            "created_at": created_at,
            "idempotency_key": self._cle_idempotence,
            "created_by": self._saisisseur(),
        }
        # Le technicien n'existe que sur l'expedition : create_return() et la
        # reception n'ont pas ce parametre, l'envoyer les ferait echouer.
        if self.doc_type == "DELIVERY":
            payload_envoi["technician"] = self._technicien() or None

        if self._is_fuel:
            payload_envoi["vehicle_plate"] = self.vehicle_plate_var.get().strip() or None
            if not self._is_fuel_receiving:
                mileage_raw = self.mileage_var.get().strip()
                mileage_val = float_saisie(mileage_raw) if mileage_raw else None
                payload_envoi["project"] = self.project_var.get().strip() or None
                payload_envoi["mileage"] = mileage_val
                payload_envoi["fuel_card"] = self.fuel_card_var.get().strip() or None
                payload_envoi["receiver"] = party or None

        if self.doc_type == "RETURN":
            fn = lambda: self.api.create_return(**{k: v for k, v in payload_envoi.items() if k != "doc_type"})
        else:
            fn = lambda: self.api.create_document(**payload_envoi)
        run_async(self, fn, lambda r: self._on_submit_ok(r),
                  lambda e: self._on_submit_error(e, payload_envoi))

    def _on_submit_ok(self, resultat):
        if resultat.get("_rejeu"):
            messagebox.showinfo(
                t("Bon déjà enregistré"),
                t("{type} n°{id} était déjà enregistrée (la réponse du "
                  "premier envoi s'était perdue).\n\nAucun doublon n'a été "
                  "créé, le stock n'a été modifié qu'une fois.").format(
                      type=t(self.labels["title"]), id=resultat["id"]),
            )
        else:
            nb_lignes_resultat = len(resultat.get("lines", []))
            self._show_toast(
                t("✓ {type} n°{id} enregistré(e) — {n} ligne(s)").format(
                    type=t(self.labels["title"]), id=resultat["id"],
                    n=nb_lignes_resultat)
            )
            alertes = resultat.get("alertes_seuil") or []
            if alertes:
                msg = t("Attention — produits sous le seuil :") + "\n\n"
                for a in alertes:
                    msg += (f"  ⚠ {a['sku']} {a['name']}\n"
                            + t("     Stock : {stock:g}  /  Seuil : {seuil:g}").format(
                                stock=a["stock"], seuil=a["seuil"]) + "\n\n")
                messagebox.showwarning(t("Alerte seuil"), msg)
            avertissements = resultat.get("avertissements") or []
            if avertissements:
                messagebox.showwarning(
                    t("Vérification à faire"),
                    t("Le bon est enregistré, mais :") + "\n\n"
                    + "\n\n".join(avertissements),
                )
            self._proposer_impression(resultat)
        if self.doc_type == "RECEIVING" and self._sources_defaut is not None:
            source = self.party_var.get().strip()
            if source and source not in self._sources_defaut:
                self._sources_defaut.append(source)
                self.source_combo.configure(values=self._sources_defaut)
        self._cle_idempotence = None
        effacer_brouillon(self.doc_type)
        # L'en-tête (Source/Région/Superviseur) reste rempli : enchaîner
        # plusieurs bons vers la même destination est le cas courant.
        # Le bouton « Nouveau bon vierge » remet tout à zéro au besoin.
        self.lines.vider()
        self.tree.delete(*self.tree.get_children())
        self.reference_var.set("")
        self.note_var.set("")
        self.date_var.set(datetime.now().strftime("%Y-%m-%d %H:%M"))
        self.status_label.configure(text="")
        self._update_line_count()
        self.submit_btn.configure(state="normal", text=t(self.labels["verb"]))
        self.refresh_products()

    def _proposer_impression(self, resultat):
        """Propose d'imprimer le bon qui vient d'être enregistré.

        Question posée après CHAQUE bon : un poste qui n'imprime jamais peut
        la couper une fois pour toutes via « Ne plus demander ». La préférence
        est propre au poste (config locale), pas au compte.

        Pour le carburant, le PDF n'est pas d'abord un bordereau à signer mais
        un ticket à envoyer au receveur (WhatsApp, e-mail) : c'est le même
        fichier, le libellé de la question le dit simplement.
        """
        from config import enregistrer_preference, lire_preference
        from views.dialogues import demander_avec_memoire
        from views.print_utils import generer_pdf_bon
        from views.print_utils import est_secteur_fuel

        if not lire_preference("proposer_impression", True):
            return

        est_fuel = est_secteur_fuel(self.sector)
        if est_fuel:
            titre = t("Ticket")
            question = t(
                "{type} n°{id} enregistrée.\n\nCréer le ticket PDF à envoyer "
                "(WhatsApp, e-mail) ?")
        else:
            titre = t("Impression")
            question = t("{type} n°{id} enregistrée.\n\nImprimer ce bon maintenant ?")

        imprimer, ne_plus_demander = demander_avec_memoire(
            self.winfo_toplevel(),
            titre,
            question.format(type=t(self.labels["title"]), id=resultat["id"]),
            texte_case=t("Ne plus demander sur ce poste"),
        )
        if ne_plus_demander:
            enregistrer_preference("proposer_impression", False)
        if not imprimer:
            return
        try:
            unites = {p["id"]: p.get("unit", "pcs") for p in self.products}
            generer_pdf_bon(resultat, unites, parent=self.winfo_toplevel(),
                            secteur=self.sector)
        except Exception as e:
            messagebox.showerror(
                titre,
                t("Le bon est bien enregistré, mais le PDF n'a pas pu être "
                  "créé :\n{e}").format(e=e),
            )

    def _on_submit_error(self, exc, payload_envoi):
        erreur = str(exc)
        if "contacter le serveur" in erreur.lower() or "timeout" in erreur.lower():
            if messagebox.askyesno(
                t("Serveur injoignable"),
                t("Le serveur ne répond pas.\n\nMettre ce bon en file "
                  "d'attente ?\nIl sera envoyé automatiquement au retour du "
                  "réseau.")
                + ("\n\n" + t("⚠ Attention : le stock n'a pas été vérifié "
                               "par le serveur.")
                   if self.doc_type in ("DELIVERY", "RETURN") else ""),
            ):
                n = mettre_en_attente("document", payload_envoi, username=self.api.username)
                messagebox.showinfo(
                    t("En file d'attente"),
                    t("Bon mis en attente ({n} en file).\nIl sera envoyé dès "
                      "que le serveur sera joignable.").format(n=n),
                )
                self._cle_idempotence = None
                # Le bon est désormais conservé dans la file d'attente : garder
                # en plus un brouillon ferait ressaisir deux fois le même bon.
                effacer_brouillon(self.doc_type)
                self.lines.vider()
                self.tree.delete(*self.tree.get_children())
                # Sans cela, le compteur continuait d'annoncer « 3 ligne(s) —
                # 30 unités » sur un tableau vide, et la référence du bon parti
                # en file restait dans le champ : l'opérateur pouvait la
                # ressaisir telle quelle sur le bon suivant.
                self.reference_var.set("")
                self._update_line_count()
                self.status_label.configure(text="")
                self._notifier_file()
                self.submit_btn.configure(state="normal", text=t(self.labels["verb"]))
                return
        # Délai de garde anti-double-clic : le bouton reste désactivé 3 secondes
        self.submit_btn.configure(state="disabled", text=t("Patiente..."))
        self.after(3000, lambda: self.submit_btn.configure(
            state="normal", text=t(self.labels["verb"])))
        messagebox.showerror(t("Erreur"), erreur)

    # -------------------------------------------------------- WebSocket
    def document_cree(self, data):
        """Notification temps reel : un bon du meme type a ete cree."""
        if data.get("doc_type") == self.doc_type:
            doc_id = data.get("id")
            self._show_toast(
                t("Nouveau {type} enregistré (n°{id})").format(
                    type=t(self.labels["title"]).lower(), id=doc_id),
                SUCCESS
            )

    def document_annule(self, data):
        """Notification temps reel : un bon du meme type a ete annule."""
        if data.get("doc_type") == self.doc_type:
            doc_id = data.get("id")
            self._show_toast(
                t("{type} annulé (n°{id})").format(
                    type=t(self.labels["title"]), id=doc_id),
                DANGER
            )
