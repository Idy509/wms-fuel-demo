import json
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from async_call import run_async
from i18n import t
from recherche import correspond, normaliser
from views.photo_utils import PHOTOS_DISPONIBLES, choisir_image, miniature
from views.utils import float_saisie, sort_treeview as _sort_treeview


class ProductsView(ctk.CTkFrame):
    def __init__(self, master, api, user_role=None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self.user_role = user_role
        self._all_products: list[dict] = []
        # Rempli par `_render` ; initialisé ici pour que les actions du bas de
        # l'écran ne dépendent pas d'un premier rendu déjà passé.
        self._tree_product_map: dict = {}
        self._build()
        self.refresh()

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 8))
        ctk.CTkLabel(top, text=t("\U0001f5c4  Catalogue produits"),
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")
        # Création et import sont réservés aux administrateurs côté serveur :
        # les proposer aux autres ne mènerait qu'à un refus incompréhensible.
        self._btn_import = None
        if self.user_role in (None, "admin"):
            self._btn_import = ctk.CTkButton(
                top, text=t("\U0001f4e5  Importer Excel"), width=160,
                fg_color="#1a1d23", hover_color="#2a2d35",
                border_width=1, border_color="#2a2d35",
                corner_radius=8, command=self._import)
            self._btn_import.pack(side="right")
        self._btn_refresh = ctk.CTkButton(
            top, text=t("↻  Rafraîchir"), width=110,
            fg_color="#2a2d35", hover_color="#353840",
            corner_radius=8, command=self.refresh)
        self._btn_refresh.pack(side="right", padx=8)

        # Étiquettes : sur la sélection si l'opérateur en a une, sinon sur
        # tout ce que le filtre affiche. Réimprimer une planche entière est le
        # cas courant après un import de catalogue.
        self._btn_etiquettes = ctk.CTkButton(
            top, text=t("\U0001f3f7  Imprimer des étiquettes"), width=200,
            fg_color="#1a1d23", hover_color="#2a2d35",
            border_width=1, border_color="#2a2d35",
            corner_radius=8, command=self._imprimer_etiquettes)
        self._btn_etiquettes.pack(side="right", padx=(0, 8))

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._render())
        ctk.CTkEntry(top, textvariable=self.search_var,
                     placeholder_text=t("\U0001f50d  Rechercher..."),
                     width=240, corner_radius=8, height=32,
                     fg_color="#1a1d23", border_color="#2a2d35").pack(side="right", padx=8)

        # « Toutes » et « Pièce » / « Volume (gls) » servent à la fois de
        # libellé affiché et de sentinelle logique : on mémorise leur forme
        # traduite pour que les comparaisons restent justes en anglais.
        self._toutes = t("Toutes")
        self._label_piece = t("Pièce")
        self._label_volume = t("Volume (gls)")
        self._cat_var = tk.StringVar(value=self._toutes)
        self._cat_combo = ctk.CTkComboBox(
            top, variable=self._cat_var, width=160,
            values=[self._toutes], state="readonly",
            command=lambda _: self._render())
        self._cat_combo.pack(side="right", padx=(0, 4))

        form = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                            border_width=1, border_color="#2a2d35")
        if self.user_role in (None, "admin"):
            form.pack(fill="x", padx=20, pady=8)
        ctk.CTkLabel(form, text=t("Ajouter un produit"),
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="#f0f0f0").grid(
            row=0, column=0, columnspan=6, sticky="w", padx=8, pady=(10, 4)
        )

        self.sku_var = tk.StringVar()
        self.name_var = tk.StringVar()
        self.unit_type_var = tk.StringVar(value=self._label_piece)
        self.bidon_var = tk.StringVar()
        self.cout_var = tk.StringVar()
        self.barcode_var = tk.StringVar()

        ctk.CTkEntry(form, textvariable=self.sku_var, placeholder_text=t("SKU *"), width=130).grid(row=1, column=0, padx=6, pady=8)
        ctk.CTkEntry(form, textvariable=self.name_var, placeholder_text=t("Nom *"), width=220).grid(row=1, column=1, padx=6, pady=8)
        ctk.CTkComboBox(form, variable=self.unit_type_var,
                        values=[self._label_piece, self._label_volume],
                        width=120, state="readonly",
                        command=self._on_unit_type_change).grid(row=1, column=2, padx=6, pady=8)
        self.bidon_entry = ctk.CTkEntry(form, textvariable=self.bidon_var,
                                        placeholder_text=t("Gls/bidon"), width=90)
        self.bidon_entry.grid(row=1, column=3, padx=6, pady=8)
        ctk.CTkEntry(form, textvariable=self.cout_var,
                     placeholder_text=t("Coût unitaire"), width=100).grid(row=1, column=4, padx=6, pady=8)
        # Un produit compté à la pièce n'a pas de capacité en gallons/bidon.
        self._on_unit_type_change(self.unit_type_var.get())
        self._btn_add = ctk.CTkButton(form, text=t("Ajouter"), width=100,
                                      command=self._add_product)
        self._btn_add.grid(row=1, column=5, padx=6, pady=8)

        # Code-barres : sur sa propre ligne, jamais obligatoire. Le champ se
        # remplit aussi bien à la main qu'à la douchette (qui tape le code
        # puis Entrée) ; c'est lui qui rend le produit scannable en inventaire.
        ctk.CTkEntry(form, textvariable=self.barcode_var,
                     placeholder_text=t("Code-barres (facultatif — scanne-le ici)"),
                     width=300).grid(row=2, column=0, columnspan=2,
                                     padx=6, pady=(0, 10), sticky="w")

        info = ctk.CTkFrame(self, fg_color="transparent")
        info.pack(fill="x", padx=20, pady=(0, 4))
        ctk.CTkLabel(info,
                     text=t("Le stock affiché est en lecture seule ici : il se "
                            "modifie via Réception / Expédition / Inventaire."),
                     text_color="#6b7280", font=ctk.CTkFont(size=10)).pack(anchor="w")

        table_frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                                   border_width=1, border_color="#2a2d35")
        table_frame.pack(fill="both", expand=True, padx=20, pady=(0, 10))

        columns = ("sku", "name", "unit", "stock", "bidon", "category", "cout", "statut")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {"sku": "SKU", "name": t("Nom"), "unit": t("Unité"),
                    "stock": t("Stock"), "bidon": t("Gls/bidon"),
                    "category": t("Catégorie"), "cout": t("Coût unitaire"),
                    "statut": t("Statut")}
        widths = {"sku": 140, "name": 300, "unit": 80, "stock": 90, "bidon": 90,
                  "category": 180, "cout": 100, "statut": 90}
        for col in columns:
            self.tree.heading(col, text=headings[col],
                              command=lambda c=col: _sort_treeview(self.tree, c))
            self.tree.column(col, width=widths.get(col, 100), anchor="w")
        # Une ligne archivée reste lisible mais visiblement en retrait.
        self.tree.tag_configure("archive", foreground="#9ca3af")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", self._on_double_click)

        bottom_bar = ctk.CTkFrame(self, fg_color="transparent")
        bottom_bar.pack(fill="x", padx=16, pady=(0, 2))
        self.count_label = ctk.CTkLabel(bottom_bar, text="", text_color="#6b7280",
                                       font=ctk.CTkFont(size=11))
        self.count_label.pack(side="left")
        self._btn_archive = None
        self._btn_unarchive = None
        self._archives_var = tk.BooleanVar(value=False)
        if self.user_role == "admin":
            self._btn_archive = ctk.CTkButton(
                bottom_bar, text=t("Archiver"), width=110,
                fg_color="#7a2a2a", hover_color="#943535",
                corner_radius=6, command=self._archive_product)
            self._btn_archive.pack(side="right")
            # Sans ce bouton, un produit archivé par erreur était perdu : son
            # SKU restait réservé, impossible de le recréer ni de le revoir.
            self._btn_unarchive = ctk.CTkButton(
                bottom_bar, text=t("Réactiver"), width=110,
                fg_color="#2a5a3a", hover_color="#357045",
                corner_radius=6, command=self._unarchive_product)
            self._btn_unarchive.pack(side="right", padx=8)
            ctk.CTkCheckBox(
                bottom_bar, text=t("Afficher les archivés"),
                variable=self._archives_var, onvalue=True, offvalue=False,
                checkbox_width=18, checkbox_height=18,
                font=ctk.CTkFont(size=11), text_color="#9ca3af",
                command=self.refresh).pack(side="right", padx=12)
        self.status_label = ctk.CTkLabel(self, text="", text_color="orange")
        self.status_label.pack(anchor="w", padx=16, pady=(0, 8))

    # WebSocket methods for real-time updates
    def produit_cree(self, product_data):
        """Handle a new product created event from WebSocket."""
        self._all_products.append(product_data)
        self._render()

    def produit_mis_a_jour(self, product_data):
        """Handle a product updated event from WebSocket."""
        for i, p in enumerate(self._all_products):
            if p["id"] == product_data["id"]:
                self._all_products[i] = product_data
                break
        self._render()

    def produit_supprime(self, product_id):
        """Handle a product deleted event from WebSocket."""
        self._all_products = [p for p in self._all_products if p.get("id") != product_id]
        self._render()

    def _erreur(self, exc):
        """Affiche une erreur d'appel serveur sans figer la fenêtre."""
        if self.winfo_exists():
            self.status_label.configure(text=str(exc), text_color="orange")

    def refresh(self, message: str = ""):
        # Tous les appels serveur de cet écran passent par run_async : exécutés
        # sur la boucle Tk, ils gelaient toute l'application le temps du
        # timeout réseau (jusqu'à 30 s).
        self.status_label.configure(text=t("Chargement…"), text_color="#6b7280")

        def _ok(produits):
            self._all_products = produits
            self.status_label.configure(
                text=message, text_color="#22c55e" if message else "orange")
            cats = sorted({p.get("category") or "" for p in self._all_products} - {""})
            self._cat_combo.configure(values=[self._toutes] + cats)
            self._render()

        avec_archives = bool(self._archives_var.get())
        # all_sites=True : l'écran Produits doit montrer/gérer le catalogue
        # de TOUS les sites (secteur FUEL), contrairement à
        # Stock/Réception/Expédition/Jaugeage — un admin doit pouvoir créer
        # et corriger la fiche d'un site qui n'est pas le sien (ex. Rijkaard
        # posant la capacité du Diesel d'Orelus). Sans effet hors FUEL, et
        # ignoré côté serveur pour un compte non-admin.
        run_async(self, lambda: self.api.get_products(include_archived=avec_archives,
                                                       all_sites=True),
                  _ok, self._erreur)

    def _render(self):
        # Normalisée une seule fois ici, pas une fois par produit : `correspond`
        # renormalise sans dommage, mais sur 500 articles à chaque frappe c'est
        # 500 décompositions Unicode évitées.
        query = normaliser(self.search_var.get())
        cat_filter = self._cat_var.get()
        self.tree.delete(*self.tree.get_children())
        self._tree_product_map = {}
        shown = 0
        for p in self._all_products:
            if query and not correspond(query, p["sku"], p["name"]):
                continue
            cat = p.get("category") or ""
            if cat_filter != self._toutes and cat != cat_filter:
                continue
            bidon = f"{p['bidon_capacity']:g}" if p.get("bidon_capacity") else ""
            cout = f"{p['unit_cost']:g}" if p.get("unit_cost") is not None else ""
            stock = f"{p['current_stock']:g}" if p.get("current_stock") is not None else ""
            archive = bool(p.get("archived"))
            iid = self.tree.insert("", "end", values=(
                p["sku"], p["name"], p["unit"], stock, bidon, cat, cout,
                t("Archivé") if archive else t("Actif"),
            ), tags=("archive",) if archive else ())
            self._tree_product_map[iid] = p
            shown += 1
        total = len(self._all_products)
        if (query or cat_filter != self._toutes) and shown != total:
            self.count_label.configure(
                text=t("{shown} / {total} produits").format(shown=shown, total=total))
        else:
            self.count_label.configure(text=t("{n} produits").format(n=total))

    # ------------------------------------------------------- étiquettes

    def selection_a_etiqueter(self) -> list[dict]:
        """Produits visés par l'impression d'étiquettes.

        La sélection du tableau si elle existe (le Treeview accepte la
        sélection multiple), sinon TOUT ce que le filtre affiche à l'écran :
        imprimer ce qu'on voit est le comportement attendu, et personne ne
        sélectionne 400 lignes à la main pour une planche complète.
        """
        carte = getattr(self, "_tree_product_map", {})
        selection = [carte[iid] for iid in self.tree.selection() if iid in carte]
        if selection:
            return selection
        return [carte[iid] for iid in self.tree.get_children("") if iid in carte]

    def _imprimer_etiquettes(self):
        from views.etiquettes import imprimer_etiquettes
        produits = self.selection_a_etiqueter()
        if not produits:
            self.status_label.configure(
                text=t("Aucun produit affiché : rien à étiqueter"),
                text_color="orange")
            return
        try:
            chemin = imprimer_etiquettes(produits, parent=self.winfo_toplevel())
        except Exception as e:
            messagebox.showerror(
                t("Étiquettes"),
                t("La planche d'étiquettes n'a pas pu être créée :\n{e}").format(e=e))
            return
        if chemin:
            self.status_label.configure(
                text=t("{n} étiquette(s) générée(s)").format(n=len(produits)),
                text_color="#22c55e")

    def _on_unit_type_change(self, valeur: str):
        """Grise « Gls/bidon » pour un produit compté à la pièce."""
        if valeur == self._label_volume:
            self.bidon_entry.configure(state="normal")
        else:
            self.bidon_var.set("")
            self.bidon_entry.configure(state="disabled")

    def _add_product(self):
        sku, name = self.sku_var.get().strip(), self.name_var.get().strip()
        if not sku or not name:
            self.status_label.configure(text=t("SKU et nom sont obligatoires"))
            return

        is_volume = self.unit_type_var.get() == self._label_volume
        unit_type = "volume" if is_volume else "piece"
        unit = "gls" if is_volume else "pcs"

        bidon_capacity = None
        bidon_raw = self.bidon_var.get().strip()
        if bidon_raw:
            try:
                bidon_capacity = float_saisie(bidon_raw)
                if bidon_capacity <= 0:
                    raise ValueError
            except ValueError:
                self.status_label.configure(text=t("Capacité bidon invalide"))
                return

        unit_cost = None
        cout_raw = self.cout_var.get().strip()
        if cout_raw:
            try:
                unit_cost = float_saisie(cout_raw)
                if unit_cost < 0:
                    raise ValueError
            except ValueError:
                self.status_label.configure(text=t("Coût unitaire invalide"))
                return

        # Bouton verrouillé pendant l'envoi : sans cela un double clic crée
        # deux fois le produit et la fenêtre reste figée le temps du réseau.
        self._btn_add.configure(state="disabled", text=t("Envoi…"))
        self.status_label.configure(text=t("Création en cours…"),
                                    text_color="#6b7280")

        def _restaurer():
            if self._btn_add.winfo_exists():
                self._btn_add.configure(state="normal", text=t("Ajouter"))

        def _ok(_res):
            _restaurer()
            self.sku_var.set("")
            self.name_var.set("")
            self.bidon_var.set("")
            self.cout_var.set("")
            self.barcode_var.set("")
            self.unit_type_var.set(self._label_piece)
            self._on_unit_type_change(self._label_piece)
            self.status_label.configure(text="")
            self.refresh()

        def _err(exc):
            _restaurer()
            self._erreur(exc)

        run_async(self,
                  lambda: self.api.create_product(
                      sku, name, unit, 0, 0,
                      unit_type=unit_type, bidon_capacity=bidon_capacity,
                      unit_cost=unit_cost,
                      barcode=self.barcode_var.get().strip() or None),
                  _ok, _err)

    def _import(self):
        path = filedialog.askopenfilename(filetypes=[("Excel", "*.xlsx")])
        if not path:
            return

        # L'import Excel est l'opération la plus longue de l'écran : sans
        # run_async, toute l'application reste figée pendant l'upload.
        if self._btn_import is not None:
            self._btn_import.configure(state="disabled",
                                       text=t("Import en cours…"))
        self.status_label.configure(text=t("Import en cours, patiente…"),
                                    text_color="#6b7280")

        def _restaurer():
            if self._btn_import is not None and self._btn_import.winfo_exists():
                self._btn_import.configure(state="normal",
                                           text=t("\U0001f4e5  Importer Excel"))

        def _ok(result):
            _restaurer()
            self.status_label.configure(text="")
            self.refresh()
            messagebox.showinfo(t("Import terminé"), self._resume_import(result))

        def _err(exc):
            _restaurer()
            self.status_label.configure(text="")
            messagebox.showerror(t("Erreur"), str(exc))

        run_async(self, lambda: self.api.import_products(path), _ok, _err)

    @staticmethod
    def _resume_import(result: dict) -> str:
        lignes = [
            t("{crees} produit(s) créé(s), {maj} mis à jour.").format(
                crees=result["created"], maj=result["updated"]),
        ]
        if result.get("lignes_lues"):
            lignes.append(t("{lues} ligne(s) lue(s) → {refs} référence(s) "
                            "unique(s).").format(
                                lues=result["lignes_lues"],
                                refs=result.get("references_uniques", 0)))

        ecarts = result.get("ecarts") or []
        if ecarts:
            lignes.append("")
            lignes.append(t("⚠ {n} écart(s) de stock constaté(s), NON "
                            "appliqué(s) :").format(n=len(ecarts)))
            for e in ecarts[:5]:
                lignes.append(t("   {sku} : système {systeme:g}, "
                                "fichier {fichier:g}").format(
                                    sku=e["sku"], systeme=e["stock_systeme"],
                                    fichier=e["stock_fichier"]))
            if len(ecarts) > 5:
                lignes.append(t("   … et {n} autre(s)").format(n=len(ecarts) - 5))
            lignes.append(t("   Corrige-les par un bon d'inventaire si le "
                            "fichier fait foi."))

        avertissements = result.get("avertissements") or []
        if avertissements:
            lignes.append("")
            lignes.append(t("Signalements :"))
            lignes.extend(f"   • {a}" for a in avertissements[:8])
            if len(avertissements) > 8:
                lignes.append(t("   … et {n} autre(s)").format(
                    n=len(avertissements) - 8))

        erreurs = result.get("erreurs") or []
        if erreurs:
            lignes.append("")
            lignes.append(t("⚠ {n} ligne(s) rejetée(s) :").format(n=len(erreurs)))
            lignes.extend(f"   • {e}" for e in erreurs[:8])
            if len(erreurs) > 8:
                lignes.append(t("   … et {n} autre(s)").format(n=len(erreurs) - 8))

        return "\n".join(lignes)

    def _archive_product(self):
        sel = self.tree.selection()
        if not sel:
            self.status_label.configure(text=t("Sélectionne un produit à archiver"))
            return
        product = self._tree_product_map.get(sel[0])
        if not product:
            return
        if not messagebox.askyesno(
            t("Archiver le produit"),
            t("Archiver « {sku} — {nom} » ?\n\nLe produit ne sera plus "
              "visible dans les listes et formulaires.\nCette action est "
              "réversible en base de données.").format(
                  sku=product["sku"], nom=product["name"]),
        ):
            return
        if self._btn_archive is not None:
            self._btn_archive.configure(state="disabled", text=t("Envoi…"))
        self.status_label.configure(text=t("Archivage en cours…"),
                                    text_color="#6b7280")

        def _restaurer():
            if self._btn_archive is not None and self._btn_archive.winfo_exists():
                self._btn_archive.configure(state="normal", text=t("Archiver"))

        def _ok(_res):
            _restaurer()
            self.refresh(t("Produit « {sku} » archivé").format(
                sku=product["sku"]))

        def _err(exc):
            _restaurer()
            self.status_label.configure(text="")
            messagebox.showerror(t("Erreur"), str(exc))

        run_async(self, lambda: self.api.archive_product(product["id"]), _ok, _err)

    def _unarchive_product(self):
        """Remet un produit archivé dans le catalogue actif."""
        sel = self.tree.selection()
        if not sel:
            self.status_label.configure(
                text=t("Coche « Afficher les archivés », puis sélectionne "
                       "le produit à réactiver"),
                text_color="orange")
            return
        product = self._tree_product_map.get(sel[0])
        if not product:
            return
        if not product.get("archived"):
            self.status_label.configure(
                text=t("« {sku} » n'est pas archivé : rien à réactiver").format(
                    sku=product["sku"]),
                text_color="orange")
            return
        if not messagebox.askyesno(
            t("Réactiver le produit"),
            t("Réactiver « {sku} — {nom} » ?\n\nLe produit réapparaîtra "
              "dans les listes et les formulaires de bons.").format(
                  sku=product["sku"], nom=product["name"]),
        ):
            return
        self._btn_unarchive.configure(state="disabled", text=t("Envoi…"))
        self.status_label.configure(text=t("Réactivation en cours…"),
                                    text_color="#6b7280")

        def _restaurer():
            if self._btn_unarchive is not None and self._btn_unarchive.winfo_exists():
                self._btn_unarchive.configure(state="normal", text=t("Réactiver"))

        def _ok(_res):
            _restaurer()
            self.refresh(t("Produit « {sku} » réactivé").format(
                sku=product["sku"]))

        def _err(exc):
            _restaurer()
            self.status_label.configure(text="")
            messagebox.showerror(t("Erreur"), str(exc))

        run_async(self, lambda: self.api.unarchive_product(product["id"]), _ok, _err)

    def _on_double_click(self, event):
        sel = self.tree.selection()
        if not sel:
            return
        product = self._tree_product_map.get(sel[0])
        if not product:
            return
        if self.user_role != "admin":
            messagebox.showinfo(
                t("Accès refusé"),
                t("Seul un administrateur peut modifier un produit."))
            return
        EditProductDialog(self, self.api, product, on_saved=self.refresh)


class EditProductDialog(ctk.CTkToplevel):
    def __init__(self, master, api, product: dict, on_saved=None):
        super().__init__(master)
        self.api = api
        self.product = product
        self.on_saved = on_saved
        self._label_piece = t("Pièce")
        self._label_volume = t("Volume (gls)")
        self.title(t("Modifier — {sku}").format(sku=product["sku"]))
        # Hauteur bornée à l'écran disponible (moins une marge) : le contenu
        # du formulaire dépasse largement une fenêtre fixe sur un écran
        # modeste (14+ champs, section photo comprise) — sans défilement, le
        # bouton Enregistrer se retrouvait hors écran, inatteignable.
        hauteur = min(760, self.winfo_screenheight() - 80)
        self.geometry(f"440x{hauteur}")
        self.minsize(440, 400)

        self.update_idletasks()
        x = master.winfo_rootx() + 100
        y = max(master.winfo_rooty() - 30, 10)
        self.geometry(f"440x{hauteur}+{x}+{y}")

        self.transient(master)
        self.lift()
        self.grab_set()

        ctk.CTkLabel(self, text=t("Modifier {sku}").format(sku=product["sku"]),
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(16, 8))

        # Le formulaire (et la section photo) défilent ; le statut et les
        # boutons d'action restent fixes en bas, TOUJOURS visibles quelle
        # que soit la taille de l'écran.
        contenu = ctk.CTkScrollableFrame(self, fg_color="transparent")
        contenu.pack(fill="both", expand=True, padx=10, pady=(0, 4))

        form = ctk.CTkFrame(contenu, fg_color="transparent")
        form.pack(fill="x", padx=20)

        self.name_var = tk.StringVar(value=product["name"])
        self.sku_var = tk.StringVar(value=product["sku"])

        ctk.CTkLabel(form, text=t("SKU :")).grid(row=0, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=self.sku_var, width=200).grid(
            row=0, column=1, padx=8, pady=6, sticky="w")

        ctk.CTkLabel(form, text=t("Nom :")).grid(row=1, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=self.name_var, width=200).grid(
            row=1, column=1, padx=8, pady=6, sticky="w")

        is_volume = product.get("unit_type") == "volume"
        self.unit_type_var = tk.StringVar(
            value=self._label_volume if is_volume else self._label_piece)
        ctk.CTkLabel(form, text=t("Unité :")).grid(row=2, column=0, sticky="w", pady=6)
        ctk.CTkComboBox(form, variable=self.unit_type_var,
                        values=[self._label_piece, self._label_volume],
                        width=140, state="readonly",
                        command=self._on_unit_type_change).grid(
            row=2, column=1, padx=8, pady=6, sticky="w")

        self.bidon_var = tk.StringVar(
            value=f"{product['bidon_capacity']:g}" if product.get("bidon_capacity") else "")
        ctk.CTkLabel(form, text=t("Gls/bidon :")).grid(row=3, column=0, sticky="w", pady=6)
        self.bidon_entry = ctk.CTkEntry(form, textvariable=self.bidon_var, width=100)
        self.bidon_entry.grid(row=3, column=1, padx=8, pady=6, sticky="w")
        # Un produit compté à la pièce n'a pas de capacité en gallons/bidon.
        self._on_unit_type_change(self.unit_type_var.get())

        # Consigne bouteille : champ dédié, sans rapport avec Gls/bidon.
        # Rempli seulement pour les produits dont les contenants vides
        # doivent revenir à l'entrepôt (ex. eau distillée, 5 gls/bouteille).
        self.consigne_var = tk.StringVar(
            value=f"{product['consigne_bouteille']:g}"
            if product.get("consigne_bouteille") else "")
        ctk.CTkLabel(form, text=t("Consigne bouteille\n(gallons/bouteille) :"),
                     justify="left").grid(row=4, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=self.consigne_var, width=100,
                     placeholder_text=t("vide = aucune")).grid(
            row=4, column=1, padx=8, pady=6, sticky="w")

        self.cout_var = tk.StringVar(
            value=f"{product['unit_cost']:g}" if product.get("unit_cost") is not None else "")
        ctk.CTkLabel(form, text=t("Coût unitaire :")).grid(row=5, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=self.cout_var, width=100,
                     placeholder_text=t("vide = non renseigné")).grid(
            row=5, column=1, padx=8, pady=6, sticky="w")

        # La catégorie n'était modifiable nulle part : déduite du nom à la
        # création, elle restait fausse ou vide pour toujours. C'est pourtant
        # elle qui regroupe les produits dans le tableau de bord.
        self.categorie_var = tk.StringVar(value=product.get("category") or "")
        ctk.CTkLabel(form, text=t("Catégorie :")).grid(row=6, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=self.categorie_var, width=200,
                     placeholder_text=t("vide = non classé")).grid(
            row=6, column=1, padx=8, pady=6, sticky="w")

        # Code-barres : le champ où l'on pose la douchette pour associer une
        # étiquette à cette fiche. Vidé = le code est retiré du produit.
        self.barcode_var = tk.StringVar(value=product.get("barcode") or "")
        ctk.CTkLabel(form, text=t("Code-barres :")).grid(
            row=7, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=self.barcode_var, width=200,
                     placeholder_text=t("vide = aucun")).grid(
            row=7, column=1, padx=8, pady=6, sticky="w")

        # ------------------------------------------------- champs FON
        # Apportés par le Masterlist FON (Part Number, Groupe, Sous-catégorie,
        # Fournisseur, Modèles supportés, RL_Apply, Discontinued) : sans objet
        # pour un produit Consumables classique, laissés vides dans ce cas.
        self.part_number_var = tk.StringVar(value=product.get("part_number") or "")
        ctk.CTkLabel(form, text=t("Part number :")).grid(row=8, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=self.part_number_var, width=200).grid(
            row=8, column=1, padx=8, pady=6, sticky="w")

        self.vendor_var = tk.StringVar(value=product.get("vendor") or "")
        ctk.CTkLabel(form, text=t("Fournisseur :")).grid(row=9, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=self.vendor_var, width=200).grid(
            row=9, column=1, padx=8, pady=6, sticky="w")

        self.group_name_var = tk.StringVar(value=product.get("group_name") or "")
        ctk.CTkLabel(form, text=t("Groupe :")).grid(row=10, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=self.group_name_var, width=200).grid(
            row=10, column=1, padx=8, pady=6, sticky="w")

        self.sub_category_var = tk.StringVar(value=product.get("sub_category") or "")
        ctk.CTkLabel(form, text=t("Sous-catégorie :")).grid(row=11, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=self.sub_category_var, width=200).grid(
            row=11, column=1, padx=8, pady=6, sticky="w")

        self.genset_models_var = tk.StringVar(
            value=product.get("supported_genset_models") or "")
        ctk.CTkLabel(form, text=t("Modèles supportés :")).grid(
            row=12, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=self.genset_models_var, width=200).grid(
            row=12, column=1, padx=8, pady=6, sticky="w")

        self.rl_apply_var = tk.BooleanVar(value=bool(product.get("rl_apply")))
        ctk.CTkCheckBox(form, text=t("RL_Apply"), variable=self.rl_apply_var,
                        onvalue=True, offvalue=False).grid(
            row=13, column=0, sticky="w", pady=6)
        self.discontinued_var = tk.BooleanVar(value=bool(product.get("discontinued")))
        ctk.CTkCheckBox(form, text=t("Discontinued"), variable=self.discontinued_var,
                        onvalue=True, offvalue=False).grid(
            row=13, column=1, sticky="w", pady=6)

        # Capacité de cuve (secteur FUEL, ex. Diesel plafonné à 1300 gal) :
        # une réception qui la dépasserait est refusée par le serveur. Vide =
        # pas de plafond (ex. Gasoline).
        self.tank_capacity_var = tk.StringVar(
            value=f"{product['tank_capacity']:g}"
            if product.get("tank_capacity") else "")
        ctk.CTkLabel(form, text=t("Capacité de cuve\n(gallons) :"),
                     justify="left").grid(row=14, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=self.tank_capacity_var, width=100,
                     placeholder_text=t("vide = aucun plafond")).grid(
            row=14, column=1, padx=8, pady=6, sticky="w")

        # Site physique (secteur FUEL, ex. "WH Central", "Canapé-Vert") :
        # filtre quels comptes non-admin voient et peuvent utiliser ce
        # produit. Vide = visible par tous les comptes du secteur.
        self.site_var = tk.StringVar(value=product.get("site") or "")
        ctk.CTkLabel(form, text=t("Site :")).grid(row=15, column=0, sticky="w", pady=6)
        ctk.CTkEntry(form, textvariable=self.site_var, width=200,
                     placeholder_text=t("vide = visible par tous")).grid(
            row=15, column=1, padx=8, pady=6, sticky="w")

        # -------------------------------------------------------- photo
        # Sous le formulaire et pas au milieu : l'identification visuelle aide
        # à reconnaître un article en rayon, elle ne conditionne aucune saisie.
        self._photo_widget = None
        self._image_courante = None  # référence gardée : Tk ne la retient pas
        cadre_photo = ctk.CTkFrame(contenu, fg_color="#1a1d23", corner_radius=8)
        cadre_photo.pack(fill="x", padx=20, pady=(6, 10))
        self._photo_label = ctk.CTkLabel(
            cadre_photo, text=t("Aucune photo"), text_color="#6b7280",
            font=ctk.CTkFont(size=11), width=120, height=90)
        self._photo_label.pack(side="left", padx=10, pady=8)
        boutons_photo = ctk.CTkFrame(cadre_photo, fg_color="transparent")
        boutons_photo.pack(side="left", padx=6)
        if PHOTOS_DISPONIBLES:
            ctk.CTkButton(boutons_photo, text=t("📷  Changer la photo"), width=170,
                          height=26, font=ctk.CTkFont(size=11),
                          fg_color="#2a2d35", hover_color="#353840",
                          command=self._changer_photo).pack(pady=2)
            self._btn_suppr_photo = ctk.CTkButton(
                boutons_photo, text=t("Retirer la photo"), width=170, height=26,
                font=ctk.CTkFont(size=11), fg_color="#3f2a2a",
                hover_color="#5b3232", command=self._retirer_photo)
            self._btn_suppr_photo.pack(pady=2)
        else:
            self._btn_suppr_photo = None
            ctk.CTkLabel(boutons_photo,
                         text=t("Affichage des photos indisponible sur ce poste"),
                         text_color="#6b7280",
                         font=ctk.CTkFont(size=10)).pack(pady=2)
        self._charger_photo()

        self.status_lbl = ctk.CTkLabel(self, text="", text_color="orange")
        self.status_lbl.pack()

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=8)
        # « Historique » avant les deux boutons d'action : on consulte la vie
        # de l'article AVANT de décider d'y toucher. Il n'enregistre rien, donc
        # il ne ferme pas ce dialogue — les deux fenêtres cohabitent.
        ctk.CTkButton(btn_frame, text=t("\U0001f552  Historique"), width=130,
                      fg_color="#2a2d35", hover_color="#353840",
                      command=self._ouvrir_historique).pack(side="left", padx=8)
        ctk.CTkButton(btn_frame, text=t("Annuler"), width=100,
                      fg_color="#2a2d35", hover_color="#353840",
                      command=self.destroy).pack(side="left", padx=8)
        self._bouton = ctk.CTkButton(btn_frame, text=t("Enregistrer"), width=140,
                                     fg_color="#2563eb", hover_color="#1d4ed8",
                                     command=self._save)
        self._bouton.pack(side="left", padx=8)

    def _on_unit_type_change(self, valeur: str):
        """Grise « Gls/bidon » pour un produit compté à la pièce."""
        if valeur == self._label_volume:
            self.bidon_entry.configure(state="normal")
        else:
            self.bidon_var.set("")
            self.bidon_entry.configure(state="disabled")

    def _ouvrir_historique(self):
        """Fiche « vie du produit », dans sa propre fenêtre."""
        HistoriqueProduitDialog(self, self.api, self.product)

    # ------------------------------------------------------------- photo

    def _maj_bouton_retrait(self):
        if self._btn_suppr_photo is None:
            return
        etat = "normal" if self.product.get("photo_path") else "disabled"
        self._btn_suppr_photo.configure(state=etat)

    def _charger_photo(self):
        """Télécharge et affiche la photo, sans bloquer l'ouverture du dialogue."""
        self._maj_bouton_retrait()
        if not PHOTOS_DISPONIBLES or not self.product.get("photo_path"):
            self._photo_label.configure(image=None, text=t("Aucune photo"))
            return
        self._photo_label.configure(image=None, text=t("Chargement…"))

        def _ok(octets):
            if not self.winfo_exists():
                return
            image = miniature(octets, 120, 90) if octets else None
            if image is None:
                self._photo_label.configure(image=None, text=t("Photo illisible"))
                return
            # Référence conservée sur l'instance : sans ça, le ramasse-miettes
            # récupère l'image et le cadre reste vide.
            self._image_courante = image
            self._photo_label.configure(image=image, text="")

        def _err(_exc):
            if self.winfo_exists():
                self._photo_label.configure(image=None, text=t("Photo indisponible"))

        run_async(self,
                  lambda: self.api.get_product_photo(self.product["id"]),
                  _ok, _err)

    def _changer_photo(self):
        chemin = choisir_image(self, t("Photo du produit"))
        if not chemin:
            return
        self._photo_label.configure(image=None, text=t("Envoi…"))

        def _ok(resultat):
            self.product["photo_path"] = resultat.get("photo_path")
            if self.winfo_exists():
                self._charger_photo()

        def _err(exc):
            if self.winfo_exists():
                self._photo_label.configure(image=None, text=t("Aucune photo"))
                messagebox.showerror(t("Photo refusée"), str(exc), parent=self)

        run_async(self,
                  lambda: self.api.upload_product_photo(self.product["id"], chemin),
                  _ok, _err)

    def _retirer_photo(self):
        if not self.product.get("photo_path"):
            return
        if not messagebox.askyesno(
                t("Retirer la photo"),
                t("Retirer la photo de « {sku} » ?").format(sku=self.product["sku"]),
                parent=self):
            return

        def _ok(_res):
            self.product["photo_path"] = None
            if self.winfo_exists():
                self._image_courante = None
                self._charger_photo()

        def _err(exc):
            if self.winfo_exists():
                messagebox.showerror(t("Erreur"), str(exc), parent=self)

        run_async(self,
                  lambda: self.api.delete_product_photo(self.product["id"]),
                  _ok, _err)

    def _save(self):
        sku = self.sku_var.get().strip()
        name = self.name_var.get().strip()
        if not sku or not name:
            self.status_lbl.configure(text=t("SKU et nom obligatoires"))
            return

        is_volume = self.unit_type_var.get() == self._label_volume
        unit_type = "volume" if is_volume else "piece"
        unit = "gls" if is_volume else "pcs"

        bidon_capacity = None
        bidon_raw = self.bidon_var.get().strip()
        if bidon_raw:
            try:
                bidon_capacity = float_saisie(bidon_raw)
                if bidon_capacity <= 0:
                    raise ValueError
            except ValueError:
                self.status_lbl.configure(text=t("Capacité bidon invalide"))
                return

        consigne = None
        consigne_raw = self.consigne_var.get().strip()
        if consigne_raw:
            try:
                consigne = float_saisie(consigne_raw)
                if consigne <= 0:
                    raise ValueError
            except ValueError:
                self.status_lbl.configure(
                    text=t("Consigne bouteille invalide (nombre de gallons > 0)"))
                return

        cout = None
        cout_raw = self.cout_var.get().strip()
        if cout_raw:
            try:
                cout = float_saisie(cout_raw)
                if cout < 0:
                    raise ValueError
            except ValueError:
                self.status_lbl.configure(text=t("Coût unitaire invalide"))
                return

        tank_capacity = None
        tank_raw = self.tank_capacity_var.get().strip()
        if tank_raw:
            try:
                tank_capacity = float_saisie(tank_raw)
                if tank_capacity <= 0:
                    raise ValueError
            except ValueError:
                self.status_lbl.configure(
                    text=t("Capacité de cuve invalide (nombre de gallons > 0)"))
                return

        # Toujours envoyés, meme vides : un champ laisse vide veut dire
        # « efface la valeur », pas « ne change rien ». Sinon une capacite
        # bidon ou une consigne saisie par erreur resterait en base.
        kwargs = {
            "sku": sku,
            "name": name,
            "unit": unit,
            "unit_type": unit_type,
            "bidon_capacity": bidon_capacity,
            "consigne_bouteille": consigne,
            "unit_cost": cout,
            "category": self.categorie_var.get().strip() or None,
            "barcode": self.barcode_var.get().strip() or None,
            "part_number": self.part_number_var.get().strip() or None,
            "vendor": self.vendor_var.get().strip() or None,
            "group_name": self.group_name_var.get().strip() or None,
            "sub_category": self.sub_category_var.get().strip() or None,
            "supported_genset_models": self.genset_models_var.get().strip() or None,
            "rl_apply": bool(self.rl_apply_var.get()),
            "discontinued": bool(self.discontinued_var.get()),
            "tank_capacity": tank_capacity,
            "site": self.site_var.get().strip() or None,
        }

        # Bouton verrouillé pendant l'appel : sans cela, un double clic part
        # deux fois et la fenêtre reste figée le temps du réseau.
        self._bouton.configure(state="disabled", text=t("Enregistrement…"))

        def _ok(_res):
            if self.on_saved:
                self.on_saved()
            self.destroy()

        def _err(exc):
            if self.winfo_exists():
                self._bouton.configure(state="normal", text=t("Enregistrer"))
                self.status_lbl.configure(text=str(exc))

        run_async(self,
                  lambda: self.api.update_product(self.product["id"], **kwargs),
                  _ok, _err)


class HistoriqueProduitDialog(ctk.CTkToplevel):
    """Toute la vie d'un article : mouvements et changements de fiche mêlés.

    Une seule liste chronologique, alimentée par `/products/{id}/historique`.
    L'intérêt tient précisément au mélange : un changement de seuil la veille
    d'un comptage, une modification de fiche entre deux réceptions, c'est
    l'enchaînement qui explique un écart — et il était invisible tant qu'il
    fallait ouvrir trois écrans séparés pour le reconstituer.

    Lecture seule. Aucun bouton n'agit sur le produit : on vient ici pour
    comprendre, pas pour corriger.
    """

    # Assez pour couvrir plusieurs mois d'un article courant sans faire
    # attendre l'ouverture. Le serveur plafonne de son côté.
    LIMITE = 200

    def __init__(self, master, api, product: dict):
        super().__init__(master)
        self.api = api
        self.product = product
        self.title(t("Historique — {sku}").format(sku=product.get("sku") or ""))
        self.geometry("900x520")
        self.minsize(700, 380)

        self.update_idletasks()
        try:
            x = master.winfo_rootx() + 40
            y = master.winfo_rooty() + 40
            self.geometry(f"900x520+{x}+{y}")
        except tk.TclError:
            # Fenêtre parente déjà détruite : la position par défaut fait
            # l'affaire, ce n'est pas une raison pour ne rien afficher.
            pass

        self.transient(master)
        self.lift()
        # Pas de grab_set : cette fenêtre n'est qu'une consultation, la fiche
        # produit doit rester utilisable derrière elle.

        entete = ctk.CTkFrame(self, fg_color="transparent")
        entete.pack(fill="x", padx=16, pady=(14, 6))
        ctk.CTkLabel(
            entete,
            text=t("{sku} — {nom}").format(sku=product.get("sku") or "",
                                           nom=product.get("name") or ""),
            font=ctk.CTkFont(size=15, weight="bold"),
            text_color="#f0f0f0").pack(side="left")
        ctk.CTkButton(entete, text=t("↻  Rafraîchir"), width=110,
                      fg_color="#2a2d35", hover_color="#353840",
                      corner_radius=8, command=self.refresh).pack(side="right")

        cadre = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                             border_width=1, border_color="#2a2d35")
        cadre.pack(fill="both", expand=True, padx=16, pady=(0, 6))

        colonnes = ("date", "categorie", "libelle", "detail", "auteur")
        self.tree = ttk.Treeview(cadre, columns=colonnes, show="headings")
        entetes = {"date": t("Date"), "categorie": t("Nature"),
                   "libelle": t("Évènement"), "detail": t("Détail"),
                   "auteur": t("Par")}
        largeurs = {"date": 130, "categorie": 100, "libelle": 170,
                    "detail": 320, "auteur": 130}
        for col in colonnes:
            self.tree.heading(col, text=entetes[col],
                              command=lambda c=col: _sort_treeview(self.tree, c))
            self.tree.column(col, width=largeurs[col], anchor="w")
        # Un bon annulé reste dans la liste mais visiblement en retrait : le
        # masquer laisserait un trou inexpliqué dans la chronologie.
        self.tree.tag_configure("annule", foreground="#9ca3af")
        self.tree.tag_configure("fiche", foreground="#60a5fa")
        self.tree.pack(side="left", fill="both", expand=True)
        barre = ttk.Scrollbar(cadre, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=barre.set)
        barre.pack(side="right", fill="y")

        self.status_lbl = ctk.CTkLabel(self, text="", text_color="#6b7280",
                                       font=ctk.CTkFont(size=11),
                                       wraplength=850, justify="left")
        self.status_lbl.pack(anchor="w", padx=16, pady=(0, 10))

        self.refresh()

    # ------------------------------------------------------------ données

    def refresh(self):
        self.status_lbl.configure(text=t("Chargement…"), text_color="#6b7280")

        def _ok(data):
            if self.winfo_exists():
                self._afficher(data)

        def _err(exc):
            if self.winfo_exists():
                self.status_lbl.configure(text=str(exc), text_color="orange")

        run_async(self,
                  lambda: self.api.get_historique_produit(
                      self.product["id"], limite=self.LIMITE),
                  _ok, _err)

    def _afficher(self, data: dict):
        self.tree.delete(*self.tree.get_children())
        entrees = data.get("entrees") or []
        for i, e in enumerate(entrees):
            fiche = e.get("categorie") == "fiche"
            tag = "annule" if e.get("annule") else ("fiche" if fiche else "")
            self.tree.insert("", "end", iid=str(i), tags=(tag,) if tag else (),
                             values=(
                (e.get("date") or "")[:16],
                t("Fiche") if fiche else t("Mouvement"),
                e.get("libelle") or "",
                self._detail(e),
                e.get("auteur") or "—",
            ))

        if not entrees:
            self.status_lbl.configure(
                text=t("Aucun mouvement ni changement de fiche pour cet "
                       "article : il n'a encore rien vécu."),
                text_color="#6b7280")
            return
        self.status_lbl.configure(
            text=t("{n} évènement(s), du plus récent au plus ancien. "
                   "En bleu : les changements de fiche. En gris : les bons "
                   "annulés, conservés pour que la chronologie reste "
                   "complète. Stock actuel : {stock}.").format(
                       n=len(entrees),
                       stock=self._nombre(data.get("current_stock"))),
            text_color="#9ca3af")

    # ------------------------------------------------------------- rendu

    @staticmethod
    def _nombre(valeur) -> str:
        try:
            return f"{float(valeur):g}"
        except (TypeError, ValueError):
            return "—"

    def _detail(self, entree: dict) -> str:
        """Une phrase courte qui dit ce qui s'est passé, sans jargon."""
        if entree.get("categorie") == "fiche":
            return self._detail_fiche(entree)

        morceaux = [t("Bon n° {id}").format(id=entree.get("document_id"))]
        quantite = entree.get("quantite")
        if quantite is not None:
            morceaux.append(t("{q} {u}").format(
                q=self._nombre(quantite), u=self.product.get("unit") or ""))
        recue = entree.get("quantite_recue")
        if recue is not None and quantite is not None and abs(recue - quantite) > 1e-9:
            morceaux.append(t("reçu {q} (écart {e})").format(
                q=self._nombre(recue), e=f"{recue - quantite:+g}"))
        source = entree.get("source_region")
        region = entree.get("region")
        if source and region:
            morceaux.append(t("{source} → {region}").format(
                source=source, region=region))
        elif region:
            morceaux.append(str(region))
        if entree.get("technician"):
            morceaux.append(str(entree["technician"]))
        if entree.get("reference"):
            morceaux.append(t("réf. {r}").format(r=entree["reference"]))
        if entree.get("annule"):
            morceaux.append(t("ANNULÉ"))
        return " · ".join(m for m in morceaux if m)

    @staticmethod
    def _detail_fiche(entree: dict) -> str:
        """Champs réellement modifiés, ancienne valeur → nouvelle.

        `old_values` / `new_values` arrivent en JSON texte (tels qu'écrits
        dans le journal d'audit). Illisibles ou absents, on n'affiche rien
        plutôt qu'un message d'erreur : l'évènement lui-même reste la vraie
        information, et un dialogue de consultation ne doit jamais échouer
        sur une ligne mal formée.
        """
        try:
            avant = json.loads(entree.get("old_values") or "{}")
            apres = json.loads(entree.get("new_values") or "{}")
        except (ValueError, TypeError):
            return ""
        if not isinstance(apres, dict):
            return ""
        if not isinstance(avant, dict):
            avant = {}
        changements = []
        for champ, valeur in apres.items():
            ancien = avant.get(champ)
            if champ in avant and ancien == valeur:
                continue
            if champ in avant:
                changements.append(
                    "{c} : {a} → {n}".format(
                        c=champ,
                        a="—" if ancien in (None, "") else ancien,
                        n="—" if valeur in (None, "") else valeur))
            else:
                changements.append(f"{champ} : {valeur}")
        return ", ".join(changements[:6])
