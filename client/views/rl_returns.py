"""Reverse logistics (RL) : suivi de l'équipement prêté dû par région.

Concerne les produits marqués RL_Apply (ex. les routeurs GPON FON) : une
livraison crée automatiquement une attente de retour, suivie ici jusqu'à ce
qu'elle soit soldée. Distinct du Retour (RETURN), qui rend du stock produit —
ici on ne touche jamais au stock, on suit uniquement l'équipement dû.
"""
import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk

import reference_data
from async_call import run_async
from i18n import t
from views.utils import float_saisie

VUE_REGION = "Région"
VUE_TECHNICIEN = "Technicien"
AUCUN_TECHNICIEN = "Aucun"


class RlReturnsView(ctk.CTkFrame):

    def __init__(self, master, api, user_role: str = "operateur"):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self._user_role = user_role
        self._balances: list[dict] = []
        self._ledger: list[dict] = []
        self._produits: dict[str, int] = {}
        self._vue_region = t(VUE_REGION)
        self._vue_technicien = t(VUE_TECHNICIEN)
        self._aucun_technicien = t(AUCUN_TECHNICIEN)
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ vue

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 4))
        ctk.CTkLabel(top, text=t("\U0001f504  Retours équipement (RL)"),
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")
        ctk.CTkButton(top, text=t("↻  Rafraîchir"), width=110,
                      fg_color="#2a2d35", hover_color="#353840",
                      corner_radius=8, command=self.refresh).pack(side="right")

        ctk.CTkLabel(self,
                     text=t("Suivi de l'équipement prêté (reverse logistics) "
                            "dû par région — livré et pas encore retourné."),
                     text_color="#6b7280", font=ctk.CTkFont(size=11)).pack(
            anchor="w", padx=20, pady=(0, 4))

        choix = ctk.CTkFrame(self, fg_color="transparent")
        choix.pack(fill="x", padx=20, pady=(0, 6))
        ctk.CTkLabel(choix, text=t("Regrouper par :"), text_color="#9ca3af",
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 8))
        self.vue_var = tk.StringVar(value=self._vue_region)
        self.vue_selecteur = ctk.CTkSegmentedButton(
            choix, values=[self._vue_region, self._vue_technicien],
            variable=self.vue_var,
            command=lambda _v: self.refresh())
        self.vue_selecteur.pack(side="left")

        table_frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                                   border_width=1, border_color="#2a2d35")
        table_frame.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        columns = ("region", "technician", "product", "quantity")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {"region": t("Région"), "technician": t("Technicien"),
                    "product": t("Produit"), "quantity": t("Unités dues")}
        widths = {"region": 160, "technician": 170, "product": 300,
                  "quantity": 140}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col],
                             anchor="e" if col == "quantity" else "w")
        self.tree.tag_configure("du", foreground="#f59e0b")
        self.tree.tag_configure("avance", foreground="#6b7280")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical",
                                  command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        self._build_form()

        if self._user_role == "admin":
            self._build_ledger()

        self.status_label = ctk.CTkLabel(self, text="", text_color="#6b7280",
                                         font=ctk.CTkFont(size=11))
        self.status_label.pack(anchor="w", padx=20, pady=(0, 10))

    def _build_form(self):
        form = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                            border_width=1, border_color="#2a2d35")
        form.pack(fill="x", padx=20, pady=(0, 6))

        ctk.CTkLabel(form, text=t("Enregistrer un retour d'équipement"),
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="#f0f0f0").grid(
            row=0, column=0, columnspan=6, sticky="w", padx=10, pady=(10, 2))

        ctk.CTkLabel(form, text=t("Équipement rapporté physiquement par la "
                                  "région. Le stock produit n'est pas "
                                  "modifié."),
                     text_color="#6b7280", font=ctk.CTkFont(size=10)).grid(
            row=1, column=0, columnspan=6, sticky="w", padx=10, pady=(0, 6))

        self.product_var = tk.StringVar()
        self.region_var = tk.StringVar()
        self.quantity_var = tk.StringVar()
        self.technicien_var = tk.StringVar(value=self._aucun_technicien)
        self.reference_var = tk.StringVar()

        ctk.CTkLabel(form, text=t("Produit"), text_color="#9ca3af",
                     font=ctk.CTkFont(size=11)).grid(row=2, column=0, sticky="w", padx=10)
        self.product_combo = ctk.CTkComboBox(form, variable=self.product_var,
                                             values=[], width=280, state="readonly")
        self.product_combo.grid(row=3, column=0, padx=10, pady=(0, 12), sticky="w")

        ctk.CTkLabel(form, text=t("Région"), text_color="#9ca3af",
                     font=ctk.CTkFont(size=11)).grid(row=2, column=1, sticky="w", padx=6)
        self.region_combo = ctk.CTkComboBox(form, variable=self.region_var,
                                            values=reference_data.regions(),
                                            width=180, state="readonly",
                                            command=self._maj_techniciens)
        self.region_combo.grid(row=3, column=1, padx=6, pady=(0, 12), sticky="w")

        # Peuplé selon la région choisie : les responsables d'équipe FON
        # (fichier Team FON) sont le référentiel superviseur/région, pas une
        # liste fixe. Choisir Port-au-Prince doit proposer ses 3 responsables
        # de zone, choisir Carrefour son seul responsable, etc.
        ctk.CTkLabel(form, text=t("Technicien (facultatif)"), text_color="#9ca3af",
                     font=ctk.CTkFont(size=11)).grid(row=2, column=2, sticky="w", padx=6)
        self.technicien_combo = ctk.CTkComboBox(
            form, variable=self.technicien_var,
            values=[self._aucun_technicien],
            width=180, state="readonly")
        self.technicien_combo.grid(row=3, column=2, padx=6, pady=(0, 12), sticky="w")

        ctk.CTkLabel(form, text=t("Quantité"), text_color="#9ca3af",
                     font=ctk.CTkFont(size=11)).grid(row=2, column=3, sticky="w", padx=6)
        ctk.CTkEntry(form, textvariable=self.quantity_var, width=140).grid(
            row=3, column=3, padx=6, pady=(0, 12), sticky="w")

        ctk.CTkLabel(form, text=t("N° de série / référence (facultatif)"),
                     text_color="#9ca3af",
                     font=ctk.CTkFont(size=11)).grid(row=2, column=4, sticky="w", padx=6)
        self.reference_entry = ctk.CTkEntry(form, textvariable=self.reference_var,
                                            width=160)
        self.reference_entry.grid(row=3, column=4, padx=6, pady=(0, 12), sticky="w")

        self._btn_enregistrer = ctk.CTkButton(
            form, text=t("✓  Enregistrer le retour"), width=190,
            fg_color="#2563eb", hover_color="#1d4ed8",
            corner_radius=8, command=self._enregistrer)
        self._btn_enregistrer.grid(row=3, column=5, padx=10, pady=(0, 12), sticky="w")

        if self._user_role == "lecteur":
            self._btn_enregistrer.configure(state="disabled")
            self.product_combo.configure(state="disabled")
            self.region_combo.configure(state="disabled")
            self.technicien_combo.configure(state="disabled")
            self.reference_entry.configure(state="disabled")
            ctk.CTkLabel(form, text=t("Lecture seule : un lecteur ne peut pas "
                                      "enregistrer de retour"),
                        text_color="#6b7280", font=ctk.CTkFont(size=10)).grid(
                row=4, column=0, columnspan=6, sticky="w", padx=10, pady=(0, 8))

    def _maj_techniciens(self, _region=None):
        """Repeuple le combo Technicien avec les responsables de la région choisie.

        Port-au-Prince n'a pas d'entrepôt régional (livraison directe à l'un
        des responsables de zone) : contrairement à Carrefour/Arcahaie/North/
        South, il n'y a personne d'autre à qui attribuer le retour, donc pas
        de choix « Aucun » — un responsable doit être désigné.
        """
        region = self.region_var.get().strip()
        superviseurs = reference_data.supervisors_for(region) if region else []
        if region == "Port-au-Prince" and superviseurs:
            valeurs = superviseurs
            if self.technicien_var.get() not in valeurs:
                self.technicien_var.set(valeurs[0])
        else:
            valeurs = [self._aucun_technicien] + superviseurs
            if self.technicien_var.get() not in valeurs:
                self.technicien_var.set(self._aucun_technicien)
        self.technicien_combo.configure(values=valeurs)

    def _build_ledger(self):
        """Section historique des retours avec bouton d'annulation (admin)."""
        cadre = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                             border_width=1, border_color="#2a2d35")
        cadre.pack(fill="x", padx=20, pady=(0, 6))

        entete = ctk.CTkFrame(cadre, fg_color="transparent")
        entete.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(entete, text=t("Derniers retours d'équipement"),
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")

        cols_ledger = ("id", "date", "region", "technician", "product",
                       "quantity", "reference", "statut", "note", "by")
        self.tree_ledger = ttk.Treeview(cadre, columns=cols_ledger,
                                         show="headings", height=5)
        headings_l = {"id": "#", "date": t("Date"), "region": t("Région"),
                      "technician": t("Technicien"), "product": t("Produit"),
                      "quantity": t("Quantité"), "reference": t("Référence"),
                      "statut": t("Statut"), "note": t("Note"), "by": t("Par")}
        widths_l = {"id": 50, "date": 130, "region": 120, "technician": 140,
                    "product": 180, "quantity": 90, "reference": 110,
                    "statut": 90, "note": 130, "by": 90}
        for col in cols_ledger:
            self.tree_ledger.heading(col, text=headings_l[col])
            self.tree_ledger.column(col, width=widths_l[col],
                                    anchor="e" if col == "quantity" else "w")
        self.tree_ledger.tag_configure("annule", foreground="#6b7280")
        self.tree_ledger.pack(fill="x", padx=10, pady=(0, 6))

        btn_frame = ctk.CTkFrame(cadre, fg_color="transparent")
        btn_frame.pack(fill="x", padx=10, pady=(0, 10))
        self._btn_annuler = ctk.CTkButton(
            btn_frame, text=t("Annuler le retour sélectionné"),
            width=220, fg_color="#dc2626", hover_color="#b91c1c",
            corner_radius=8, command=self._annuler_retour)
        self._btn_annuler.pack(side="left")

    # -------------------------------------------------------------- données

    def _erreur(self, exc):
        if self.winfo_exists():
            self.status_label.configure(text=str(exc), text_color="orange")

    def refresh(self):
        self._charger_produits()

        par_technicien = self.vue_var.get() == self._vue_technicien

        def _ok(balances):
            self._balances = balances
            self._render()
            if self._user_role == "admin":
                self._refresh_ledger()

        run_async(self,
                  lambda: self.api.get_rl_balance(par_technicien=par_technicien),
                  _ok, self._erreur)

    def _charger_produits(self):
        """Ne propose que les produits réellement soumis à la RL."""
        def _ok(produits):
            self._produits = {}
            for p in produits:
                if not p.get("rl_apply"):
                    continue
                libelle = f"{p['sku']} — {p['name']}"
                self._produits[libelle] = p["id"]

            valeurs = sorted(self._produits)
            self.product_combo.configure(values=valeurs)
            if valeurs and self.product_var.get() not in self._produits:
                self.product_var.set(valeurs[0])
            elif not valeurs:
                self.product_var.set("")

        run_async(self, self.api.get_products, _ok, lambda _e: None)

    def _render(self):
        for item in self.tree.get_children():
            self.tree.delete(item)

        par_technicien = self.vue_var.get() == self._vue_technicien
        cle_tri = "technician" if par_technicien else "region"
        lignes = sorted(self._balances,
                        key=lambda b: (b.get(cle_tri) or "", b.get("name") or ""))
        for b in lignes:
            solde = b.get("balance") or 0
            tag = "du" if solde > 1e-9 else "avance"
            self.tree.insert("", "end", tags=(tag,), values=(
                b.get("region") or "",
                b.get("technician") or "",
                f"{b.get('sku', '')} — {b.get('name', '')}",
                f"{solde:g}",
            ))

        if not lignes and par_technicien:
            self.status_label.configure(
                text=t("Aucun équipement dû par un technicien."),
                text_color="#6b7280")
        elif not lignes:
            self.status_label.configure(
                text=t("Aucun équipement dû. Coche RL_Apply sur un produit "
                       "dans Produits pour activer le suivi."),
                text_color="#6b7280")
        else:
            total = sum(b.get("balance") or 0 for b in lignes)
            self.status_label.configure(
                text=t("{n} ligne(s) — {total:g} unité(s) due(s) au "
                       "total").format(n=len(lignes), total=total),
                text_color="#6b7280")

    def _refresh_ledger(self):
        def _ok(ledger):
            self._ledger = ledger
            self._render_ledger()

        run_async(self, self.api.get_rl_ledger, _ok, lambda _e: _ok([]))

    def _render_ledger(self):
        for item in self.tree_ledger.get_children():
            self.tree_ledger.delete(item)
        for entry in self._ledger:
            annule = bool(entry.get("cancelled"))
            self.tree_ledger.insert("", "end", tags=("annule",) if annule else (), values=(
                entry["id"],
                entry.get("created_at", ""),
                entry.get("region", ""),
                entry.get("technician") or "",
                f"{entry.get('sku', '')} — {entry.get('name', '')}",
                f"{abs(entry.get('quantity', 0)):g}",
                entry.get("reference") or "",
                t("Annulé") if annule else t("Valide"),
                entry.get("note", "") or "",
                entry.get("created_by", "") or "",
            ))

    def _annuler_retour(self):
        selection = self.tree_ledger.selection()
        if not selection:
            messagebox.showinfo(t("Annulation"),
                                t("Sélectionne une ligne à annuler."))
            return
        item = selection[0]
        values = self.tree_ledger.item(item, "values")
        entry_id = int(values[0])

        if "annule" in self.tree_ledger.item(item, "tags"):
            messagebox.showinfo(
                t("Annulation"),
                t("Le retour #{id} est déjà annulé.").format(id=entry_id))
            return

        if not messagebox.askyesno(
            t("Confirmer l'annulation"),
            t("Annuler le retour #{id} ?\n\n"
              "Région : {region}\n"
              "Technicien : {technicien}\n"
              "Produit : {produit}\n"
              "Quantité : {quantite}\n\n"
              "L'équipement sera à nouveau compté comme dû.").format(
                  id=entry_id, region=values[2], technicien=values[3] or "—",
                  produit=values[4], quantite=values[5]),
        ):
            return

        self._btn_annuler.configure(state="disabled", text=t("Annulation…"))

        def _restaurer():
            if self._btn_annuler.winfo_exists():
                self._btn_annuler.configure(
                    state="normal", text=t("Annuler le retour sélectionné"))

        def _ok(result):
            _restaurer()
            messagebox.showinfo(
                t("Retour annulé"),
                t("Retour #{id} annulé.\nNouveau solde : {solde:g} "
                  "unité(s) dues.").format(
                      id=entry_id, solde=result.get("balance", 0)),
            )
            self.refresh()

        def _err(exc):
            _restaurer()
            messagebox.showerror(t("Erreur"), str(exc))

        run_async(self, lambda: self.api.cancel_rl_return(entry_id), _ok, _err)

    # ------------------------------------------------------------- actions

    def _enregistrer(self):
        libelle = self.product_var.get().strip()
        product_id = self._produits.get(libelle)
        if not product_id:
            self.status_label.configure(
                text=t("Choisis un produit soumis à la RL"), text_color="orange")
            return

        region = self.region_var.get().strip()
        if not region:
            self.status_label.configure(text=t("Choisis une région"),
                                        text_color="orange")
            return

        technicien = self.technicien_var.get().strip()
        if technicien == self._aucun_technicien:
            technicien = ""

        reference = self.reference_var.get().strip()

        raw = self.quantity_var.get().strip()
        try:
            quantity = float_saisie(raw)
        except ValueError:
            self.status_label.configure(
                text=t("Quantité invalide"), text_color="orange")
            return
        if quantity <= 0:
            self.status_label.configure(
                text=t("La quantité doit être supérieure à zéro"),
                text_color="orange")
            return

        self._btn_enregistrer.configure(state="disabled", text=t("Envoi…"))
        self.status_label.configure(text=t("Enregistrement en cours…"),
                                    text_color="#6b7280")

        def _restaurer():
            if self._btn_enregistrer.winfo_exists():
                self._btn_enregistrer.configure(
                    state="normal", text=t("✓  Enregistrer le retour"))

        def _ok(result):
            _restaurer()
            self.quantity_var.set("")
            self.reference_var.set("")
            rendu_par = technicien or region
            detail_reference = (t("Référence : {ref}.").format(ref=reference) + "\n"
                                if reference else "")
            detail_technicien = ""
            if technicien and result.get("technician_balance") is not None:
                detail_technicien = "\n" + t(
                    "Solde personnel de {technicien} : {solde:g} "
                    "unité(s).").format(
                        technicien=technicien,
                        solde=result["technician_balance"])
            messagebox.showinfo(
                t("Retour enregistré"),
                t("{n:g} unité(s) retournée(s) par {par}.").format(
                    n=quantity, par=rendu_par) + "\n"
                + detail_reference
                + t("Solde restant pour la région {region} : {solde:g} "
                    "unité(s).").format(
                        region=region, solde=result.get("balance", 0))
                + detail_technicien)
            self.refresh()

        def _err(exc):
            _restaurer()
            erreur = str(exc)
            if "contacter le serveur" in erreur.lower() or "timeout" in erreur.lower():
                messagebox.showerror(
                    t("Serveur injoignable"),
                    t("Le serveur ne répond pas.\n\n"
                      "Vérifie la connexion réseau et réessaie."),
                )
            else:
                messagebox.showerror(t("Retour d'équipement impossible"), erreur)
            self.status_label.configure(text=erreur, text_color="orange")

        run_async(self,
                  lambda: self.api.create_rl_return(product_id, region, quantity,
                                                    technician=technicien,
                                                    reference=reference),
                  _ok, _err)
