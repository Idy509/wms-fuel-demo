import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from api_client import ApiError
from async_call import run_async
from i18n import t
from file_attente import nombre_en_attente, rejouer
from views.utils import sort_treeview as _sort_treeview

# Le WebSocket porte le temps réel (voir client/websocket_client.py) : ce
# polling n'est plus qu'un filet de sécurité si la socket tombe.
REFRESH_MS = 30000
BACKOFF_MAX_MS = 30000
# Tant que le WebSocket est connecté, ce polling est redondant avec le temps
# réel : l'espacer réduit la charge serveur sans rien perdre, puisque le
# WebSocket porte déjà les mises à jour. Revient à REFRESH_MS dès que la
# connexion tombe (voir _replanifier).
REFRESH_MS_WS_ACTIF = 120000


class DashboardView(ctk.CTkFrame):
    def __init__(self, master, api, on_queue_change=None, on_alerte_change=None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self._on_queue_change = on_queue_change
        self._on_alerte_change = on_alerte_change
        self._all_products: list[dict] = []
        self._backoff = REFRESH_MS
        self._job = None
        # Re-rendu différé déclenché par les messages WebSocket (anti-rebond)
        self._job_render = None
        self._was_offline = False
        self._build()
        # Pas de premier chargement ici : `main.py` construit la vue PUIS
        # l'affiche (`pack`), donc à cet instant elle n'est pas encore mappée
        # et `_poll()` se contenterait de se reprogrammer sans rien charger.
        # C'est `main.py::_show()` qui appelle `refresh()` juste après le
        # `pack()` — voir la note dans `refresh()`.

    def _build(self):
        # Deux lignes plutôt qu'une : entassés sur une seule barre, les 3
        # boutons d'export + les 3 filtres + la catégorie + la recherche
        # débordaient sur un écran pas assez large, coupant "Ruptures".
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 4))

        ctk.CTkLabel(top, text=t("📦  Stock en temps réel"),
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")

        ctk.CTkButton(top, text=t("\U0001f4c4  Export PDF"), width=130,
                      fg_color="#1a1d23", hover_color="#2a2d35",
                      border_width=1, border_color="#2a2d35",
                      corner_radius=8, command=self._export_pdf).pack(side="right", padx=(0, 6))
        ctk.CTkButton(top, text=t("\U0001f4e5  Exporter Excel"), width=150,
                      fg_color="#1a1d23", hover_color="#2a2d35",
                      border_width=1, border_color="#2a2d35",
                      corner_radius=8, command=self._export).pack(side="right")
        ctk.CTkButton(top, text=t("\U0001f4cb  Réappro"), width=120,
                      fg_color="#1a1d23", hover_color="#2a2d35",
                      border_width=1, border_color="#2a2d35",
                      corner_radius=8, command=self._export_reappro).pack(side="right", padx=(0, 6))

        barre2 = ctk.CTkFrame(self, fg_color="transparent")
        barre2.pack(fill="x", padx=20, pady=(0, 8))

        self._filtre_stock = tk.StringVar(value="Tous")
        filtre_frame = ctk.CTkFrame(barre2, fg_color="transparent")
        filtre_frame.pack(side="left")
        self._filtre_btns: dict[str, ctk.CTkButton] = {}
        # Largeur par étiquette : un width unique de 70px coupait "Ruptures"
        # (8 caractères), le plus long des trois libellés.
        largeurs_filtre = {"Tous": 64, "Alertes": 78, "Ruptures": 88}
        for label in ("Tous", "Alertes", "Ruptures"):
            btn = ctk.CTkButton(filtre_frame, text=t(label),
                                width=largeurs_filtre[label], height=28,
                                corner_radius=6, fg_color="#2563eb" if label == "Tous" else "#2a2d35",
                                hover_color="#1d4ed8" if label == "Tous" else "#353840",
                                font=ctk.CTkFont(size=11),
                                command=lambda l=label: self._set_filtre(l))
            btn.pack(side="left", padx=2)
            self._filtre_btns[label] = btn

        # Anti-rebond : sans lui, chaque frappe reconstruisait tout le tableau
        # (plusieurs centaines de lignes), ce qui saccadait la saisie.
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._rafraichir_bientot())
        search_entry = ctk.CTkEntry(barre2, textvariable=self.search_var,
                                    placeholder_text=t("🔍  Rechercher un produit..."),
                                    width=240, corner_radius=8, height=32,
                                    fg_color="#1a1d23", border_color="#2a2d35")
        search_entry.pack(side="right")

        # « Toutes » sert de sentinelle « pas de filtre » ET de libellé
        # affiché : on mémorise sa forme traduite pour que la comparaison
        # dans _render() reste juste quelle que soit la langue.
        self._toutes = t("Toutes")
        self._cat_var = tk.StringVar(value=self._toutes)
        self._cat_combo = ctk.CTkComboBox(
            barre2, variable=self._cat_var, width=160,
            values=[self._toutes], state="readonly",
            command=lambda _: self._rafraichir_bientot())
        self._cat_combo.pack(side="right", padx=(0, 8))

        table_frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                                   border_width=1, border_color="#2a2d35")
        table_frame.pack(fill="both", expand=True, padx=20, pady=(0, 20))

        columns = ("sku", "name", "category", "unit", "stock", "min_stock")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {"sku": "SKU", "name": t("Nom"), "category": t("Catégorie"),
                    "unit": t("Unité"), "stock": t("Stock actuel"),
                    "min_stock": t("Seuil min")}
        widths = {"sku": 120, "name": 280, "category": 150, "unit": 80,
                  "stock": 120, "min_stock": 100}
        for col in columns:
            self.tree.heading(col, text=headings[col],
                              command=lambda c=col: _sort_treeview(self.tree, c))
            self.tree.column(col, width=widths[col], anchor="w")

        self.tree.tag_configure("critical", background="#5a2323")
        self.tree.tag_configure("warning", background="#5a4a1a")

        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self.count_label = ctk.CTkLabel(self, text="", text_color="#6b7280",
                                       font=ctk.CTkFont(size=11))
        self.count_label.pack(anchor="w", padx=16, pady=(0, 2))

        self.alerte_label = ctk.CTkLabel(self, text="", text_color="gray")
        self.alerte_label.pack(anchor="w", padx=16, pady=(0, 2))

        self.status_label = ctk.CTkLabel(self, text="", text_color="gray")
        self.status_label.pack(anchor="w", padx=16, pady=(0, 8))

    def maj_stock_produit(self, product_id: int, new_stock: float):
        """Met à jour l'affichage d'un produit spécifique lorsque son stock change via WebSocket."""
        # Mettre à jour le produit dans notre liste locale
        for product in self._all_products:
            if product["id"] == product_id:
                product["current_stock"] = new_stock
                break

        self._rafraichir_bientot()

    def _rafraichir_bientot(self):
        """Regroupe une rafale de mises à jour en un seul re-rendu.

        Un bon de 15 lignes déclenche 15 messages `stock_update` d'affilée :
        reconstruire le tableau à chaque message le faisait clignoter et
        perdait la sélection de l'opérateur. Les données locales sont déjà à
        jour ; seul l'affichage attend ce court délai.
        """
        if not self.winfo_exists():
            return
        if self._job_render is not None:
            try:
                self.after_cancel(self._job_render)
            except Exception:
                pass
        self._job_render = self.after(300, self._render_differe)

    def _render_differe(self):
        self._job_render = None
        if self.winfo_exists():
            self._render()

    def _poll(self):
        if not self.winfo_exists():
            return
        if not self.winfo_ismapped():
            self._replanifier()
            return
        self._charger()

    def refresh(self):
        """Charge le stock immédiatement (affichage de la vue, F5).

        Contrairement à `_poll()`, ne teste pas `winfo_ismapped()` : appelée
        par `main.py::_show()` juste après le `pack()`, Tk n'a pas encore
        mappé le widget (le mapping se fait dans la file « idle »), alors que
        c'est bien la vue que l'opérateur regarde.
        """
        if not self.winfo_exists():
            return
        # Annuler le cycle déjà programmé : sinon deux requêtes se croisent.
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        self._charger()

    def _charger(self):
        self.status_label.configure(text=t("Chargement..."), text_color="#6b7280")
        run_async(self, self.api.get_stock, self._on_stock, self._on_error)

    def _on_stock(self, products):
        self._all_products = products
        from datetime import datetime
        self._backoff = REFRESH_MS
        heure = datetime.now().strftime("%H:%M:%S")
        self.status_label.configure(text=t("Dernière maj : {heure}").format(heure=heure),
                                    text_color="#6b7280")
        cats = sorted({p.get("category") or "" for p in products} - {""})
        self._cat_combo.configure(values=[self._toutes] + cats)
        self._render()
        if self._was_offline and nombre_en_attente() > 0:
            run_async(self, lambda: rejouer(self.api),
                      self._on_rejeu_termine, lambda _e: None)
        self._was_offline = False
        self._replanifier()

    def _on_rejeu_termine(self, resultats):
        if not resultats:
            return
        reussis = sum(1 for r in resultats if r.get("ok"))
        echecs = [r for r in resultats if not r.get("ok")]
        msg = t("{n} bon(s) en attente envoyé(s) avec succès.").format(n=reussis)
        if echecs:
            details = "\n".join(f"- {r.get('erreur', '?')}" for r in echecs)
            msg += "\n\n" + t("{n} rejeté(s) par le serveur :\n{details}").format(
                n=len(echecs), details=details)
        from tkinter import messagebox
        messagebox.showinfo(t("File d'attente synchronisée"), msg)
        if self._on_queue_change:
            self._on_queue_change()
        self._poll()

    def _on_error(self, exc):
        self._was_offline = True
        # Back-off exponentiel : on n'inonde pas un serveur éteint.
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_MS)
        self.status_label.configure(
            text=t("Serveur injoignable — nouvelle tentative dans {s} s "
                   "(affichage : dernière synchronisation connue)").format(
                       s=self._backoff // 1000),
            text_color="orange",
        )
        self._replanifier()

    def _replanifier(self):
        if self.winfo_exists():
            delai = self._backoff
            ws = getattr(self.api, "websocket_client", None)
            if ws is not None and ws.connected:
                delai = max(delai, REFRESH_MS_WS_ACTIF)
            self._job = self.after(delai, self._poll)

    def destroy(self):
        for attribut in ("_job", "_job_render"):
            job = getattr(self, attribut, None)
            if job is not None:
                try:
                    self.after_cancel(job)
                except Exception:
                    pass
                setattr(self, attribut, None)
        super().destroy()

    def _set_filtre(self, filtre: str):
        self._filtre_stock.set(filtre)
        for label, btn in self._filtre_btns.items():
            if label == filtre:
                btn.configure(fg_color="#2563eb", hover_color="#1d4ed8")
            else:
                btn.configure(fg_color="#2a2d35", hover_color="#353840")
        self._render()

    def _render(self):
        query = self.search_var.get().strip().lower()
        filtre = self._filtre_stock.get()
        cat_filter = self._cat_var.get()
        self.tree.delete(*self.tree.get_children())

        # Les compteurs d'alertes décrivent l'état du stock COMPLET : ils
        # pilotent le badge global de l'onglet « Stock ». Les compter sur la
        # liste filtrée faisait mentir le badge dès qu'une recherche ou un
        # filtre de catégorie était actif. Le `count_label` plus bas, lui,
        # reste bien basé sur le filtre : ce sont deux compteurs différents.
        nb_critique, nb_alerte = self._compter_alertes()

        for p in self._all_products:
            categorie = p.get("category") or ""
            if cat_filter != self._toutes and categorie != cat_filter:
                continue
            if query and query not in p["sku"].lower() and query not in p["name"].lower() \
                    and query not in categorie.lower():
                continue
            seuil_min = p["min_stock"]
            seuil_critique = seuil_min / 2
            stock = p["current_stock"]
            # Rupture = stock <= 0, quel que soit le seuil. Un produit sans
            # seuil renseigné et à zéro reste une rupture : c'est la même règle
            # que la pastille du tableau de bord et que la liste d'alertes du
            # serveur. La faire dépendre de seuil_min > 0 donnait trois
            # comptages différents pour la même situation.
            rupture = stock <= 0
            if rupture or (seuil_min > 0 and stock <= seuil_critique):
                tags = ("critical",)
            elif seuil_min > 0 and stock <= seuil_min:
                tags = ("warning",)
            else:
                tags = ()

            if filtre == "Ruptures" and not rupture:
                continue
            if filtre == "Alertes" and not tags:
                continue

            unite = p.get("unit", "pcs")
            self.tree.insert("", "end", tags=tags, values=(
                p["sku"], p["name"], categorie, unite, stock, seuil_min,
            ))
        shown = len(self.tree.get_children())
        total = len(self._all_products)
        if filtre != "Tous" or cat_filter != self._toutes or (query and shown != total):
            self.count_label.configure(
                text=t("{shown} / {total} produits").format(shown=shown, total=total))
        else:
            self.count_label.configure(text=t("{n} produits").format(n=total))
        self._afficher_compteur_alertes(nb_critique, nb_alerte)

    def _compter_alertes(self) -> tuple[int, int]:
        """Compte critiques et bas sur le stock complet, hors filtre d'affichage.

        Même règle que le rendu du tableau : rupture = stock <= 0 quel que
        soit le seuil.
        """
        nb_critique = 0
        nb_alerte = 0
        for p in self._all_products:
            seuil_min = p["min_stock"]
            stock = p["current_stock"]
            if stock <= 0 or (seuil_min > 0 and stock <= seuil_min / 2):
                nb_critique += 1
            elif seuil_min > 0 and stock <= seuil_min:
                nb_alerte += 1
        return nb_critique, nb_alerte

    def _afficher_compteur_alertes(self, nb_critique: int, nb_alerte: int):
        parties = []
        if nb_critique:
            parties.append(t("{n} critique(s)").format(n=nb_critique))
        if nb_alerte:
            parties.append(t("{n} bas").format(n=nb_alerte))
        if parties:
            couleur = "#d94040" if nb_critique else "orange"
            self.alerte_label.configure(
                text=t("⚠ Stock : {parties}").format(
                    parties=", ".join(parties)), text_color=couleur,
            )
        else:
            self.alerte_label.configure(text=t("Tous les stocks sont normaux."),
                                        text_color="gray")
        if self._on_alerte_change:
            self._on_alerte_change(nb_critique, nb_alerte)

    def _export(self):
        query = self.search_var.get().strip().lower()
        cat_filter = self._cat_var.get()
        if cat_filter != self._toutes:
            init_name = f"stock_{cat_filter}.xlsx"
        elif query:
            init_name = f"stock_{query}.xlsx"
        else:
            init_name = "stock.xlsx"
        path = filedialog.asksaveasfilename(defaultextension=".xlsx",
                                            filetypes=[("Excel", "*.xlsx")],
                                            initialfile=init_name)
        if not path:
            return
        if query or cat_filter != self._toutes:
            try:
                from openpyxl import Workbook
                wb = Workbook()
                ws = wb.active
                ws.title = "Stock"
                ws.append(["SKU", t("Nom"), t("Catégorie"), t("Unité"),
                           t("Stock actuel"), t("Seuil min")])
                for child in self.tree.get_children():
                    ws.append(list(self.tree.item(child, "values")))
                for col in ws.columns:
                    ws.column_dimensions[col[0].column_letter].width = 20
                wb.save(path)
                shown = len(self.tree.get_children())
                messagebox.showinfo(t("Export"), t("{n} produit(s) exporté(s) vers {path}").format(
                    n=shown, path=path))
            except Exception as e:
                messagebox.showerror(t("Erreur"), str(e))
        else:
            try:
                self.api.export_stock(path)
                messagebox.showinfo(t("Export"), t("Stock exporté vers {path}").format(path=path))
            except ApiError as e:
                messagebox.showerror(t("Erreur"), str(e))
            except (OSError, PermissionError) as e:
                # Cas courant : le fichier est déjà ouvert dans Excel.
                messagebox.showerror(
                    t("Erreur"),
                    t("Impossible d'écrire le fichier :\n{e}\n\nVérifie qu'il "
                      "n'est pas déjà ouvert dans un autre programme.").format(e=e))

    def _export_reappro(self):
        """Exporte la liste des produits sous leur seuil, avec la quantité à
        commander — le rapport que le bureau utilise pour passer commande."""
        from datetime import datetime
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx")],
            initialfile=f"reapprovisionnement_{datetime.now().strftime('%Y%m%d')}.xlsx",
        )
        if not path:
            return
        try:
            self.api.export_reappro_report(path)
            messagebox.showinfo(t("Export"),
                                t("Rapport de réapprovisionnement exporté vers {path}").format(
                                    path=path))
        except ApiError as e:
            messagebox.showerror(t("Erreur"), str(e))
        except (OSError, PermissionError) as e:
            messagebox.showerror(
                t("Erreur"),
                t("Impossible d'écrire le fichier :\n{e}\n\nVérifie qu'il "
                  "n'est pas déjà ouvert dans un autre programme.").format(e=e))

    def _export_pdf(self):
        from datetime import datetime
        path = filedialog.asksaveasfilename(
            defaultextension=".pdf",
            filetypes=[("PDF", "*.pdf")],
            initialfile=f"stock_{datetime.now().strftime('%Y%m%d')}.pdf",
        )
        if not path:
            return
        try:
            from fpdf import FPDF

            from views.pdf_utils import safe_text, setup_pdf_fonts
        except ImportError:
            messagebox.showerror(t("Erreur"),
                                 t("Le module fpdf2 n'est pas installé.\n"
                                   "Installez-le avec : pip install fpdf2"))
            return
        try:
            pdf = FPDF(orientation="L", unit="mm", format="A4")
            # Sans police Unicode, le tiret cadratin et les accents du titre
            # font échouer l'export : safe_text() les convertit alors.
            font = setup_pdf_fonts(pdf)
            T = lambda texte: safe_text(texte, font)
            pdf.set_auto_page_break(auto=True, margin=15)
            pdf.add_page()

            # Titre
            pdf.set_font(font, "B", 16)
            titre = t("Stock en temps réel — {date}").format(
                date=datetime.now().strftime("%d/%m/%Y %H:%M"))
            pdf.cell(0, 12, T(titre), new_x="LMARGIN", new_y="NEXT", align="C")
            pdf.ln(4)

            # En-tetes du tableau
            headers = ["SKU", t("Nom"), t("Catégorie"), t("Unité"),
                       t("Stock actuel"), t("Seuil min")]
            col_widths = [35, 80, 50, 25, 35, 30]
            pdf.set_font(font, "B", 9)
            pdf.set_fill_color(40, 40, 50)
            pdf.set_text_color(255, 255, 255)
            for i, h in enumerate(headers):
                pdf.cell(col_widths[i], 8, T(h), border=1, fill=True, align="C")
            pdf.ln()

            # Lignes du tableau
            pdf.set_font(font, "", 8)
            pdf.set_text_color(0, 0, 0)
            for child in self.tree.get_children():
                vals = list(self.tree.item(child, "values"))
                for i, val in enumerate(vals):
                    align = "R" if i >= 4 else "L"
                    text = str(val) if val else ""
                    # Tronquer les textes trop longs
                    max_chars = col_widths[i] // 2
                    if len(text) > max_chars:
                        text = text[:max_chars - 1] + "..."
                    pdf.cell(col_widths[i], 6, T(text), border=1, align=align)
                pdf.ln()

            # Signature en bas
            pdf.ln(8)
            # Pas de style italique enregistré pour la police WMS.
            pdf.set_font(font, "", 8)
            pdf.set_text_color(120, 120, 120)
            shown = len(self.tree.get_children())
            pdf.cell(0, 5, T(t("{n} produit(s) — Généré le {date}").format(
                n=shown, date=datetime.now().strftime("%d/%m/%Y à %H:%M:%S"))),
                     align="C")

            pdf.output(path)
            messagebox.showinfo(t("Export PDF"),
                                t("{n} produit(s) exporté(s) vers {path}").format(
                                    n=shown, path=path))
        except (OSError, PermissionError) as e:
            messagebox.showerror(
                t("Erreur"),
                t("Impossible d'écrire le fichier :\n{e}\n\nVérifie qu'il "
                  "n'est pas déjà ouvert dans un autre programme.").format(e=e))
        except Exception as e:
            messagebox.showerror(t("Erreur"), str(e))
