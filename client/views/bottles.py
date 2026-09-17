"""Consignes bouteilles : suivi des contenants vides dus par région.

Distinct du Retour (RETURN), qui rend du stock produit. Ici on ne touche
jamais au stock : on suit uniquement les bouteilles vides qu'une région doit
rapporter à l'entrepôt.
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
# Choix explicite dans la liste Technicien du formulaire de retour : les
# bouteilles rendues par une région ne sont attribuées à personne en propre.
AUCUN_TECHNICIEN = "Aucun"


class BottlesView(ctk.CTkFrame):

    def __init__(self, master, api, user_role: str = "operateur"):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self._user_role = user_role
        self._balances: list[dict] = []
        self._ledger: list[dict] = []
        # Libellé affiché dans la liste -> id du produit.
        self._produits: dict[str, int] = {}
        # Formes traduites des libellés qui servent aussi de sentinelles :
        # calculées ici, la langue n'étant fixée qu'après l'import des vues.
        self._vue_region = t(VUE_REGION)
        self._vue_technicien = t(VUE_TECHNICIEN)
        self._aucun_technicien = t(AUCUN_TECHNICIEN)
        self._build()
        self.refresh()

    # ------------------------------------------------------------------ vue

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 4))
        ctk.CTkLabel(top, text=t("\U0001f37e  Consignes bouteilles"),
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")
        ctk.CTkButton(top, text=t("↻  Rafraîchir"), width=110,
                      fg_color="#2a2d35", hover_color="#353840",
                      corner_radius=8, command=self.refresh).pack(side="right")

        ctk.CTkLabel(self,
                     text=t("Suivi des bouteilles vides dues par région pour "
                            "les produits consignés."),
                     text_color="#6b7280", font=ctk.CTkFont(size=11)).pack(
            anchor="w", padx=20, pady=(0, 4))

        # Deux lectures du même solde : la région (livraisons collectives) et
        # le technicien (livraisons nominatives). Une seule liste mélangeant
        # les deux compterait deux fois les mêmes bouteilles.
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

        columns = ("region", "technician", "product", "bottles")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {"region": t("Région"), "technician": t("Technicien"),
                    "product": t("Produit"), "bottles": t("Bouteilles dues")}
        widths = {"region": 160, "technician": 170, "product": 300,
                  "bottles": 140}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col],
                             anchor="e" if col == "bottles" else "w")
        self.tree.tag_configure("du", foreground="#f59e0b")
        self.tree.tag_configure("avance", foreground="#6b7280")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical",
                                  command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        self._build_form()

        # --- Historique des retours (admin : bouton d'annulation) ---
        if self._user_role == "admin":
            self._build_ledger()

        self.status_label = ctk.CTkLabel(self, text="", text_color="#6b7280",
                                         font=ctk.CTkFont(size=11))
        self.status_label.pack(anchor="w", padx=20, pady=(0, 10))

    def _build_form(self):
        form = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                            border_width=1, border_color="#2a2d35")
        form.pack(fill="x", padx=20, pady=(0, 6))

        ctk.CTkLabel(form, text=t("Enregistrer un retour de bouteilles"),
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="#f0f0f0").grid(
            row=0, column=0, columnspan=6, sticky="w", padx=10, pady=(10, 2))

        ctk.CTkLabel(form, text=t("Bouteilles vides rapportées physiquement "
                                  "par la région. Le stock produit n'est pas "
                                  "modifié."),
                     text_color="#6b7280", font=ctk.CTkFont(size=10)).grid(
            row=1, column=0, columnspan=6, sticky="w", padx=10, pady=(0, 6))

        self.product_var = tk.StringVar()
        self.region_var = tk.StringVar()
        self.bottles_var = tk.StringVar()
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
                                            width=180, state="readonly")
        self.region_combo.grid(row=3, column=1, padx=6, pady=(0, 12), sticky="w")

        # Facultatif : « Aucun » pour des bouteilles rendues par la région,
        # sans que personne en particulier n'en soit crédité.
        ctk.CTkLabel(form, text=t("Technicien (facultatif)"), text_color="#9ca3af",
                     font=ctk.CTkFont(size=11)).grid(row=2, column=2, sticky="w", padx=6)
        self.technicien_combo = ctk.CTkComboBox(
            form, variable=self.technicien_var,
            values=[self._aucun_technicien] + reference_data.technicians(),
            width=180, state="readonly")
        self.technicien_combo.grid(row=3, column=2, padx=6, pady=(0, 12), sticky="w")

        ctk.CTkLabel(form, text=t("Nombre de bouteilles"), text_color="#9ca3af",
                     font=ctk.CTkFont(size=11)).grid(row=2, column=3, sticky="w", padx=6)
        ctk.CTkEntry(form, textvariable=self.bottles_var, width=140).grid(
            row=3, column=3, padx=6, pady=(0, 12), sticky="w")

        # N° de la fiche papier signée à la reprise : facultatif, mais c'est
        # la seule pièce justificative si un solde est contesté plus tard.
        ctk.CTkLabel(form, text=t("N° de fiche (facultatif)"), text_color="#9ca3af",
                     font=ctk.CTkFont(size=11)).grid(row=2, column=4, sticky="w", padx=6)
        self.reference_entry = ctk.CTkEntry(form, textvariable=self.reference_var,
                                            width=140)
        self.reference_entry.grid(row=3, column=4, padx=6, pady=(0, 12), sticky="w")

        self._btn_enregistrer = ctk.CTkButton(
            form, text=t("✓  Enregistrer le retour"), width=190,
            fg_color="#2563eb", hover_color="#1d4ed8",
            corner_radius=8, command=self._enregistrer)
        self._btn_enregistrer.grid(row=3, column=5, padx=10, pady=(0, 12), sticky="w")

        if self._user_role == "lecteur":
            # Un lecteur qui remplit ce formulaire se prenait un 403 à la
            # validation : la lecture seule doit être visible avant l'envoi.
            self._btn_enregistrer.configure(state="disabled")
            self.product_combo.configure(state="disabled")
            self.region_combo.configure(state="disabled")
            self.technicien_combo.configure(state="disabled")
            self.reference_entry.configure(state="disabled")
            ctk.CTkLabel(form, text=t("Lecture seule : un lecteur ne peut pas "
                                      "enregistrer de retour"),
                        text_color="#6b7280", font=ctk.CTkFont(size=10)).grid(
                row=4, column=0, columnspan=6, sticky="w", padx=10, pady=(0, 8))

    def _build_ledger(self):
        """Section historique des retours avec bouton d'annulation (admin)."""
        cadre = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                             border_width=1, border_color="#2a2d35")
        cadre.pack(fill="x", padx=20, pady=(0, 6))

        entete = ctk.CTkFrame(cadre, fg_color="transparent")
        entete.pack(fill="x", padx=10, pady=(10, 4))
        ctk.CTkLabel(entete, text=t("Derniers retours de bouteilles"),
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")

        cols_ledger = ("id", "date", "region", "technician", "product",
                       "bottles", "reference", "statut", "note", "by")
        self.tree_ledger = ttk.Treeview(cadre, columns=cols_ledger,
                                         show="headings", height=5)
        headings_l = {"id": "#", "date": t("Date"), "region": t("Région"),
                      "technician": t("Technicien"), "product": t("Produit"),
                      "bottles": t("Bouteilles"), "reference": t("N° de fiche"),
                      "statut": t("Statut"), "note": t("Note"), "by": t("Par")}
        widths_l = {"id": 50, "date": 130, "region": 120, "technician": 140,
                    "product": 180, "bottles": 90, "reference": 110,
                    "statut": 90, "note": 130, "by": 90}
        for col in cols_ledger:
            self.tree_ledger.heading(col, text=headings_l[col])
            self.tree_ledger.column(col, width=widths_l[col],
                                    anchor="e" if col == "bottles" else "w")
        # Un retour annulé ne compte plus dans aucun solde : il doit se voir.
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
        """Affiche une erreur d'appel serveur sans figer la fenêtre."""
        if self.winfo_exists():
            self.status_label.configure(text=str(exc), text_color="orange")

    def refresh(self):
        # Tous les appels serveur de cet écran passent par run_async : exécutés
        # sur la boucle Tk, ils gelaient toute l'application le temps du
        # timeout réseau.
        self._charger_produits()

        par_technicien = self.vue_var.get() == self._vue_technicien

        def _ok(balances):
            self._balances = balances
            self._render()
            if self._user_role == "admin":
                self._refresh_ledger()

        run_async(self,
                  lambda: self.api.get_bottle_balance(par_technicien=par_technicien),
                  _ok, self._erreur)

    def _charger_produits(self):
        """Ne propose que les produits réellement suivis en consigne."""
        def _ok(produits):
            self._produits = {}
            for p in produits:
                if not p.get("consigne_bouteille"):
                    continue
                libelle = t("{sku} — {nom} ({gls:g} gls/bouteille)").format(
                    sku=p["sku"], nom=p["name"],
                    gls=p["consigne_bouteille"])
                self._produits[libelle] = p["id"]

            valeurs = sorted(self._produits)
            self.product_combo.configure(values=valeurs)
            if valeurs and self.product_var.get() not in self._produits:
                self.product_var.set(valeurs[0])
            elif not valeurs:
                self.product_var.set("")

        # Un échec de chargement du catalogue est déjà signalé par l'appel
        # au solde : on ne double pas le message ici.
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
                text=t("Aucune bouteille due par un technicien. Renseigne le "
                       "champ Technicien sur une expédition pour activer ce "
                       "suivi."),
                text_color="#6b7280")
        elif not lignes:
            self.status_label.configure(
                text=t("Aucune bouteille due. Configure la consigne d'un "
                       "produit dans Produits (double-clic) pour activer le "
                       "suivi."),
                text_color="#6b7280")
        else:
            total = sum(b.get("balance") or 0 for b in lignes)
            self.status_label.configure(
                text=t("{n} ligne(s) — {total:g} bouteille(s) due(s) au "
                       "total").format(n=len(lignes), total=total),
                text_color="#6b7280")

    def _refresh_ledger(self):
        """Charge et affiche les dernières écritures de retour."""
        def _ok(ledger):
            self._ledger = ledger
            self._render_ledger()

        run_async(self, self.api.get_bottle_ledger, _ok, lambda _e: _ok([]))

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
                f"{abs(entry.get('bottles', 0)):g}",
                entry.get("reference") or "",
                t("Annulé") if annule else t("Valide"),
                entry.get("note", "") or "",
                entry.get("created_by", "") or "",
            ))

    def _annuler_retour(self):
        """Annule le retour de bouteilles sélectionné dans le grand livre."""
        selection = self.tree_ledger.selection()
        if not selection:
            messagebox.showinfo(t("Annulation"),
                                t("Sélectionne une ligne à annuler."))
            return
        item = selection[0]
        values = self.tree_ledger.item(item, "values")
        entry_id = int(values[0])

        # Déjà annulé : le serveur répondrait 409. Le dire ici évite un
        # aller-retour et un message d'erreur là où il n'y a pas d'erreur.
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
              "Bouteilles : {bouteilles}\n\n"
              "Les bouteilles seront à nouveau comptées comme dues.").format(
                  id=entry_id, region=values[2], technicien=values[3] or "—",
                  produit=values[4], bouteilles=values[5]),
        ):
            return

        # Bouton verrouillé pendant l'envoi : l'annulation n'a pas de clé
        # d'idempotence, un double clic annulerait deux fois.
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
                  "bouteille(s) dues.").format(
                      id=entry_id, solde=result.get("balance", 0)),
            )
            self.refresh()

        def _err(exc):
            _restaurer()
            messagebox.showerror(t("Erreur"), str(exc))

        run_async(self, lambda: self.api.cancel_bottle_return(entry_id), _ok, _err)

    # ------------------------------------------------------------- actions

    def _enregistrer(self):
        libelle = self.product_var.get().strip()
        product_id = self._produits.get(libelle)
        if not product_id:
            self.status_label.configure(
                text=t("Choisis un produit consigné"), text_color="orange")
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

        raw = self.bottles_var.get().strip()
        try:
            bottles = float_saisie(raw)
        except ValueError:
            self.status_label.configure(
                text=t("Nombre de bouteilles invalide"), text_color="orange")
            return
        if bottles <= 0:
            self.status_label.configure(
                text=t("Le nombre de bouteilles doit être supérieur à zéro"),
                text_color="orange")
            return

        # L'écriture de retour n'a pas de clé d'idempotence : le bouton est
        # verrouillé pendant l'envoi, sinon un double clic enregistre deux
        # fois les mêmes bouteilles.
        self._btn_enregistrer.configure(state="disabled", text=t("Envoi…"))
        self.status_label.configure(text=t("Enregistrement en cours…"),
                                    text_color="#6b7280")

        def _restaurer():
            if self._btn_enregistrer.winfo_exists():
                self._btn_enregistrer.configure(
                    state="normal", text=t("✓  Enregistrer le retour"))

        def _ok(result):
            _restaurer()
            self.bottles_var.set("")
            self.reference_var.set("")
            rendu_par = technicien or region
            detail_fiche = (t("Fiche n° {ref}.").format(ref=reference) + "\n"
                            if reference else "")
            detail_technicien = ""
            if technicien and result.get("technician_balance") is not None:
                detail_technicien = "\n" + t(
                    "Solde personnel de {technicien} : {solde:g} "
                    "bouteille(s).").format(
                        technicien=technicien,
                        solde=result["technician_balance"])
            messagebox.showinfo(
                t("Retour enregistré"),
                t("{n:g} bouteille(s) retournée(s) par {par}.").format(
                    n=bottles, par=rendu_par) + "\n"
                + detail_fiche
                + t("Solde restant pour la région {region} : {solde:g} "
                    "bouteille(s).").format(
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
                messagebox.showerror(t("Retour de bouteilles impossible"), erreur)
            self.status_label.configure(text=erreur, text_color="orange")

        run_async(self,
                  lambda: self.api.create_bottle_return(product_id, region, bottles,
                                                        technician=technicien,
                                                        reference=reference),
                  _ok, _err)

    # WebSocket methods for real-time updates
    def solde_bouteille_mis_a_jour(self, balance_data):
        """Handle a bottle balance updated event from WebSocket."""
        # L'événement porte un solde régional : il ne dit rien des lignes
        # affichées dans la vue par technicien, qu'un rapprochement par
        # région rendrait faux.
        if self.vue_var.get() == self._vue_technicien:
            return
        # Find the balance for this region and product and update it
        region = balance_data.get("region")
        product_id = balance_data.get("product_id")
        new_balance = balance_data.get("balance")

        for b in self._balances:
            if b.get("region") == region and b.get("product_id") == product_id:
                b["balance"] = new_balance
                break
        self._render()
