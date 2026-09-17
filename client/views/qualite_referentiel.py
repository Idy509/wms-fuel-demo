"""Santé du référentiel produits — écran d'administration.

Liste les fiches produit dont la saisie est visiblement incomplète : pas de
catégorie, pas de coût unitaire, produit en volume sans capacité de bidon,
désignation vide ou identique au SKU, désignation en double.

Rien n'est corrigé automatiquement et rien n'est bloqué : le double-clic
ouvre la fiche produit, l'administrateur décide. Deux désignations très
proches peuvent parfaitement être deux références distinctes — le système
n'a pas à trancher à sa place.
"""
import tkinter as tk
from tkinter import ttk

import customtkinter as ctk

from async_call import run_async
from i18n import t
from views.products import EditProductDialog
from views.utils import sort_treeview as _sort_treeview

TOUS_LES_DEFAUTS = "Tous les défauts"

# Ordre d'affichage du filtre. Les codes sont ceux de
# server/qualite_referentiel.py ; les libellés sont retraduits ici pour que
# l'écran suive la langue du poste sans dépendre du texte renvoyé par le
# serveur (qui, lui, reste toujours en français).
DEFAUTS = (
    ("categorie_absente", "Catégorie non renseignée"),
    ("cout_absent", "Coût unitaire non renseigné"),
    ("capacite_bidon_absente", "Produit en volume sans capacité de bidon"),
    ("doublon_designation", "Désignation très proche d'un autre produit actif"),
    ("doublon_probable", "Désignation presque identique à un autre produit actif"),
    ("nom_absent", "Désignation vide"),
    ("nom_egal_sku", "Désignation identique au code SKU"),
)


class QualiteReferentielView(ctk.CTkFrame):

    def __init__(self, master, api, user_role=None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self.user_role = user_role
        self._produits: list[dict] = []
        self._visibles: list[dict] = []
        self._total_actifs = 0
        self._ligne_produit: dict[str, dict] = {}
        # Libellé traduit -> code. Construit ici et non au niveau module :
        # la langue n'est fixée qu'après l'import des vues.
        self._libelles = {t(libelle): code for code, libelle in DEFAUTS}
        self._codes_libelles = {code: t(libelle) for code, libelle in DEFAUTS}
        self._tous = t(TOUS_LES_DEFAUTS)
        self._build()

    # ----------------------------------------------------------- interface

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 8))
        ctk.CTkLabel(top, text=t("🩺  Santé du référentiel produits"),
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")
        ctk.CTkButton(top, text=t("↻  Rafraîchir"), width=110,
                      fg_color="#2a2d35", hover_color="#353840",
                      corner_radius=8, command=self.refresh).pack(side="right", padx=8)

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._render())
        ctk.CTkEntry(top, textvariable=self.search_var,
                     placeholder_text=t("\U0001f50d  Rechercher..."),
                     width=220, corner_radius=8, height=32,
                     fg_color="#1a1d23", border_color="#2a2d35").pack(side="right", padx=8)

        self.defaut_var = tk.StringVar(value=self._tous)
        ctk.CTkComboBox(top, variable=self.defaut_var,
                        values=[self._tous] + list(self._libelles),
                        width=250, height=32, corner_radius=8, state="readonly",
                        fg_color="#1a1d23", border_color="#2a2d35",
                        button_color="#2a2d35",
                        command=lambda _v: self._render()).pack(side="right", padx=8)

        aide = ctk.CTkFrame(self, fg_color="transparent")
        aide.pack(fill="x", padx=20, pady=(0, 4))
        ctk.CTkLabel(
            aide,
            text=t("Aucune correction automatique : double-cliquez une ligne "
                   "pour ouvrir la fiche produit et corriger."),
            text_color="#6b7280", font=ctk.CTkFont(size=10)).pack(anchor="w")

        table_frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                                   border_width=1, border_color="#2a2d35")
        table_frame.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        columns = ("sku", "name", "category", "cout", "defauts")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {"sku": "SKU", "name": t("Désignation"),
                    "category": t("Catégorie"), "cout": t("Coût unitaire"),
                    "defauts": t("Défauts relevés")}
        widths = {"sku": 140, "name": 280, "category": 160, "cout": 100,
                  "defauts": 460}
        for col in columns:
            self.tree.heading(col, text=headings[col],
                              command=lambda c=col: _sort_treeview(self.tree, c))
            self.tree.column(col, width=widths[col], anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical",
                                  command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._ouvrir_fiche)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=20, pady=(0, 12))
        self._status = ctk.CTkLabel(bottom, text="", font=ctk.CTkFont(size=11),
                                    text_color="#6b7280")
        self._status.pack(side="left")

    # ------------------------------------------------------------ données

    def refresh(self):
        self._status.configure(text=t("Chargement..."), text_color="#6b7280")
        run_async(self, self.api.get_qualite_referentiel,
                  self._on_data, self._on_error)

    def _on_data(self, data: dict):
        self._produits = data.get("produits") or []
        self._total_actifs = data.get("total_actifs") or 0
        self._render()

    def _on_error(self, exc):
        self._status.configure(text=str(exc), text_color="orange")

    def _filtrer(self) -> list[dict]:
        code_voulu = self._libelles.get(self.defaut_var.get())
        recherche = self.search_var.get().strip().lower()
        resultat = []
        for produit in self._produits:
            defauts = produit.get("defauts") or []
            if code_voulu and code_voulu not in defauts:
                continue
            if recherche:
                blob = " ".join(str(produit.get(k) or "") for k in
                                ("sku", "name", "category")).lower()
                if recherche not in blob:
                    continue
            resultat.append(produit)
        return resultat

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        self._ligne_produit.clear()
        self._visibles = self._filtrer()

        for i, produit in enumerate(self._visibles):
            iid = str(i)
            self._ligne_produit[iid] = produit
            cout = produit.get("unit_cost")
            self.tree.insert("", "end", iid=iid, values=(
                produit.get("sku") or "",
                produit.get("name") or "—",
                produit.get("category") or "—",
                f"{cout:g}" if isinstance(cout, (int, float)) and cout else "—",
                self._resume_defauts(produit),
            ))

        if not self._produits:
            self._status.configure(
                text=t("Aucun défaut relevé sur les {total} produits actifs.").format(
                    total=self._total_actifs),
                text_color="#4ade80")
            return

        self._status.configure(
            text=t("{affiche} fiche(s) affichée(s) — {n} en défaut sur "
                   "{total} produits actifs.").format(
                       affiche=len(self._visibles), n=len(self._produits),
                       total=self._total_actifs),
            text_color="#9ca3af")

    def _resume_defauts(self, produit: dict) -> str:
        codes = produit.get("defauts") or []
        # Le SKU du (ou des) produit(s) rapproché(s) est la seule information
        # qui rende le défaut actionnable : sans lui, l'administrateur doit
        # rechercher lui-même la fiche jumelle dans tout le catalogue.
        skus_par_code = {
            "doublon_designation": [s for s in (produit.get("doublons_sku") or []) if s],
            "doublon_probable": [
                s for s in (produit.get("doublons_probables_sku") or []) if s],
        }
        libelles = []
        for code in codes:
            libelle = self._codes_libelles.get(code, code)
            skus = skus_par_code.get(code) or []
            if skus:
                libelle += t(" (idem {skus})").format(skus=", ".join(skus))
            libelles.append(libelle)
        return " · ".join(libelles)

    # ------------------------------------------------------------- action

    def _ouvrir_fiche(self, _event=None):
        selection = self.tree.selection()
        if not selection:
            return
        produit = self._ligne_produit.get(selection[0])
        if not produit:
            return
        # Cet écran n'est atteignable que par un administrateur (la barre
        # latérale ne l'affiche pas aux autres, le serveur refuse l'appel) :
        # pas de garde supplémentaire ici, la fiche l'applique déjà.
        EditProductDialog(self, self.api, produit, on_saved=self.refresh)
