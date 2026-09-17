import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk

from async_call import run_async
from i18n import t
from views.dialogues import demander_mot_de_confirmation
from views.utils import float_saisie

# Mot à recopier pour valider la réinitialisation du stock de début. NON
# traduit : c'est un mot à reproduire à l'identique, pas un texte à lire, et
# il doit rester le même quelle que soit la langue du poste.
MOT_CONFIRMATION_RESET = "CONFIRMER"


class ThresholdsView(ctk.CTkFrame):
    """Vue admin pour définir les seuils de réapprovisionnement."""

    def __init__(self, master, api):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self._all_products: list[dict] = []
        self._entries: dict[int, tk.StringVar] = {}
        # Seuils tapés mais pas encore enregistrés. Conservés entre deux
        # rendus : filtrer la liste ne doit jamais effacer une saisie.
        self._saisies_en_cours: dict[int, str] = {}
        # Seuil serveur affiché en regard de chaque champ AU MOMENT du rendu.
        # C'est la seule référence valable pour savoir si l'opérateur a tapé
        # quelque chose : comparer à `_all_products`, qui vient d'être remplacé
        # par la réponse du serveur, faisait passer pour « modifié par moi »
        # un seuil changé depuis un autre poste (événement WebSocket
        # `thresholds_updated`) — et « Enregistrer » renvoyait alors l'ancienne
        # valeur, écrasant la modification du collègue.
        self._references_rendues: dict[int, str] = {}
        self._build()

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 8))
        ctk.CTkLabel(top, text=t("\U0001f512  Seuils de réapprovisionnement"),
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")
        ctk.CTkButton(top, text=t("\U0001f4c5  Date début global"), width=160,
                      fg_color="#1a1d23", hover_color="#2a2d35",
                      border_width=1, border_color="#2a2d35",
                      corner_radius=8, command=self._set_global_date).pack(side="right")
        ctk.CTkButton(top, text=t("↻  Rafraîchir"), width=110,
                      fg_color="#2a2d35", hover_color="#353840",
                      corner_radius=8, command=self.refresh).pack(side="right", padx=8)

        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._render())
        ctk.CTkEntry(top, textvariable=self.search_var,
                     placeholder_text=t("\U0001f50d  Rechercher..."),
                     width=240, corner_radius=8, height=32,
                     fg_color="#1a1d23", border_color="#2a2d35").pack(side="right", padx=8)

        ctk.CTkLabel(self, text=t("Définis le stock minimum pour chaque "
                                  "produit. Le tableau de bord utilisera ces "
                                  "seuils pour les alertes."),
                     text_color="#6b7280", font=ctk.CTkFont(size=11)).pack(
            anchor="w", padx=20, pady=(0, 8))

        table_frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                                   border_width=1, border_color="#2a2d35")
        table_frame.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        self._canvas = tk.Canvas(table_frame, bg="#1a1d23", highlightthickness=0)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self._canvas.yview)
        self._inner = ctk.CTkFrame(self._canvas, fg_color="#1a1d23")

        self._inner.bind("<Configure>",
                         lambda e: self._canvas.configure(scrollregion=self._canvas.bbox("all")))
        self._canvas_window = self._canvas.create_window((0, 0), window=self._inner, anchor="nw")
        self._canvas.configure(yscrollcommand=scrollbar.set)

        self._canvas.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

        self._canvas.bind("<Configure>",
                          lambda e: self._canvas.itemconfig(self._canvas_window, width=e.width))

        def _on_mousewheel(event):
            self._canvas.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self._canvas.bind("<MouseWheel>", _on_mousewheel)
        self._inner.bind("<MouseWheel>", _on_mousewheel)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=20, pady=(0, 16))
        self._status = ctk.CTkLabel(bottom, text="", font=ctk.CTkFont(size=11))
        self._status.pack(side="left")
        self._save_btn = ctk.CTkButton(bottom, text=t("Enregistrer les seuils"),
                                       width=200,
                                       fg_color="#2563eb", hover_color="#1d4ed8",
                                       corner_radius=8, command=self._save)
        self._save_btn.pack(side="right")

    def refresh(self, conserver_saisies: bool = False):
        """Recharge le catalogue sans figer la fenêtre pendant l'appel réseau."""
        if not conserver_saisies:
            self._saisies_en_cours.clear()
        self._status.configure(text=t("Chargement..."), text_color="#6b7280")

        def ok(produits):
            if not self.winfo_exists():
                return
            self._all_products = produits
            self._render()

        def echec(e):
            if not self.winfo_exists():
                return
            self._status.configure(text=str(e), text_color="orange")

        run_async(self, self.api.get_products, ok, echec)

    def _memoriser_saisies(self):
        """Retient les valeurs tapées et pas encore enregistrées.

        Une saisie est « en cours » seulement si elle diffère À LA FOIS du
        seuil affiché au rendu (sinon l'opérateur n'a rien touché) ET du seuil
        actuellement en base (sinon elle vient d'être enregistrée, ou un autre
        poste a posé la même valeur).
        """
        actuels = {p["id"]: f"{p.get('min_stock', 0):g}"
                   for p in self._all_products}
        for product_id, var in self._entries.items():
            texte = var.get().strip()
            rendue = self._references_rendues.get(product_id)
            if rendue is None:
                continue
            if texte != rendue and texte != actuels.get(product_id, rendue):
                self._saisies_en_cours[product_id] = texte
            else:
                self._saisies_en_cours.pop(product_id, None)

    def _render(self):
        # Une recherche re-rend toute la table : sans cette sauvegarde, les
        # seuils tapés mais pas encore enregistrés disparaissaient avec les
        # widgets détruits.
        self._memoriser_saisies()

        for widget in self._inner.winfo_children():
            widget.destroy()
        self._entries.clear()
        self._references_rendues.clear()

        query = self.search_var.get().strip().lower()

        header_frame = ctk.CTkFrame(self._inner, fg_color="#252830", corner_radius=0)
        header_frame.pack(fill="x")
        for i, (text, width) in enumerate([
            ("SKU", 130), (t("Produit"), 260), (t("Unité"), 60),
            (t("Stock actuel"), 110), (t("Seuil actuel"), 110),
            (t("Nouveau seuil"), 130)
        ]):
            ctk.CTkLabel(header_frame, text=text, width=width,
                         font=ctk.CTkFont(size=11, weight="bold"),
                         text_color="#9ca3af", anchor="w").grid(
                row=0, column=i, padx=(12 if i == 0 else 4, 4), pady=8)

        count = 0
        for idx, p in enumerate(self._all_products):
            if query and query not in p["sku"].lower() and query not in p["name"].lower():
                continue

            bg = "#1e2028" if count % 2 == 0 else "#1a1d23"
            row = ctk.CTkFrame(self._inner, fg_color=bg, corner_radius=0)
            row.pack(fill="x")

            stock = p["current_stock"]
            seuil = p.get("min_stock", 0)
            en_alerte = seuil > 0 and stock <= seuil

            ctk.CTkLabel(row, text=p["sku"], width=130,
                         font=ctk.CTkFont(size=11),
                         text_color="#93c5fd" if en_alerte else "#d1d5db",
                         anchor="w").grid(row=0, column=0, padx=(12, 4), pady=5)

            nom = p["name"]
            if len(nom) > 35:
                nom = nom[:34] + "…"
            ctk.CTkLabel(row, text=nom, width=260,
                         font=ctk.CTkFont(size=11), text_color="#d1d5db",
                         anchor="w").grid(row=0, column=1, padx=4, pady=5)

            ctk.CTkLabel(row, text=p.get("unit", "pcs"), width=60,
                         font=ctk.CTkFont(size=11), text_color="#6b7280",
                         anchor="w").grid(row=0, column=2, padx=4, pady=5)

            stock_color = "#ef4444" if en_alerte else "#22c55e"
            ctk.CTkLabel(row, text=f"{stock:g}", width=110,
                         font=ctk.CTkFont(size=11, weight="bold"),
                         text_color=stock_color,
                         anchor="w").grid(row=0, column=3, padx=4, pady=5)

            ctk.CTkLabel(row, text=f"{seuil:g}", width=110,
                         font=ctk.CTkFont(size=11), text_color="#9ca3af",
                         anchor="w").grid(row=0, column=4, padx=4, pady=5)

            reference = f"{seuil:g}"
            var = tk.StringVar(value=self._saisies_en_cours.get(p["id"], reference))
            self._entries[p["id"]] = var
            self._references_rendues[p["id"]] = reference
            modifie = p["id"] in self._saisies_en_cours
            entry = ctk.CTkEntry(row, textvariable=var, width=110, height=28,
                                 fg_color="#2a2d35",
                                 border_color="#f59e0b" if modifie else "#353840",
                                 corner_radius=4, font=ctk.CTkFont(size=11))
            entry.grid(row=0, column=5, padx=4, pady=5)
            count += 1

        # Les saisies non enregistrées d'un produit masqué par la recherche
        # restent en mémoire : on le signale pour éviter la mauvaise surprise.
        en_attente = len(self._saisies_en_cours)
        if en_attente:
            self._status.configure(
                text=t("{n} produit(s) — {en_attente} seuil(s) modifié(s) non "
                       "enregistré(s)").format(n=count, en_attente=en_attente),
                text_color="#f59e0b")
        else:
            self._status.configure(text=t("{n} produit(s)").format(n=count),
                                   text_color="#6b7280")

    def _save(self):
        # On repart des saisies mémorisées : elles couvrent aussi les produits
        # actuellement masqués par la recherche.
        self._memoriser_saisies()
        seuils = {p["id"]: p.get("min_stock", 0) for p in self._all_products}
        skus = {p["id"]: p["sku"] for p in self._all_products}

        changes = []
        for product_id, texte in self._saisies_en_cours.items():
            sku = skus.get(product_id, product_id)
            raw = texte.strip()
            try:
                new_min = float_saisie(raw)
            except ValueError:
                self._status.configure(
                    text=t("Seuil invalide pour « {sku} » : saisis un nombre "
                           "(exemple : 12 ou 12.5)").format(sku=sku),
                    text_color="orange")
                return
            if new_min < 0:
                self._status.configure(
                    text=t("Le seuil de « {sku} » ne peut pas être "
                           "négatif").format(sku=sku),
                    text_color="orange")
                return
            if abs(new_min - seuils.get(product_id, 0)) > 1e-9:
                changes.append({"product_id": product_id, "min_stock": new_min})

        if not changes:
            self._status.configure(text=t("Aucune modification détectée"),
                                   text_color="#6b7280")
            return

        self._save_btn.configure(state="disabled", text=t("Enregistrement..."))
        self._status.configure(text=t("Enregistrement en cours..."),
                               text_color="#6b7280")

        def restaurer_bouton():
            if self.winfo_exists():
                self._save_btn.configure(state="normal",
                                         text=t("Enregistrer les seuils"))

        def ok(result):
            restaurer_bouton()
            if not self.winfo_exists():
                return
            self._saisies_en_cours.clear()
            self._status.configure(
                text=t("{n} seuil(s) mis à jour avec succès").format(
                    n=result["updated"]),
                text_color="#22c55e")
            self.refresh()

        def echec(e):
            restaurer_bouton()
            if self.winfo_exists():
                self._status.configure(text=str(e), text_color="orange")

        run_async(self, lambda: self.api.update_thresholds(changes), ok, echec)

    def _set_global_date(self):
        GlobalDateDialog(self, self.api, on_saved=self.refresh)

    def seuils_mis_a_jour(self, _donnees=None):
        """Événement WebSocket « thresholds_updated » : un autre poste a
        modifié les seuils, on recharge la liste.

        Cette méthode était définie sur GlobalDateDialog, qui n'a pas de
        refresh() : elle n'était donc jamais appelée, et aurait levé si elle
        l'avait été. Elle appartient à la vue, seule à savoir se redessiner.

        conserver_saisies=True : ne jamais effacer un seuil tapé et pas encore
        enregistré à cause d'une mise à jour venue d'un autre poste.
        """
        if not self.winfo_exists():
            return
        self.refresh(conserver_saisies=True)


class GlobalDateDialog(ctk.CTkToplevel):

    def __init__(self, master, api, on_saved=None):
        super().__init__(master)
        from datetime import date

        self.api = api
        self.on_saved = on_saved
        # Verrou anti double-clic : l'opération de réinitialisation n'est pas
        # idempotente (elle rebase le stock de début sur le stock courant).
        # Deux clics rapides pendant l'attente réseau la lanceraient deux fois.
        self._envoi_en_cours = False
        self.title(t("Date stock début — Tous les produits"))
        self.geometry("480x320")
        self.resizable(False, False)

        self.update_idletasks()
        x = master.winfo_rootx() + 80
        y = master.winfo_rooty() + 40
        self.geometry(f"480x320+{x}+{y}")

        self.transient(master)
        self.lift()
        self.grab_set()

        ctk.CTkLabel(self, text=t("Nouvelle période de stock"),
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(16, 4))
        ctk.CTkLabel(self, text=t("Applique la date de début à tous les produits"),
                     text_color="#9ca3af", font=ctk.CTkFont(size=12)).pack(pady=(0, 12))

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=30)

        today = date.today()
        years = [str(y) for y in range(today.year - 5, today.year + 1)]
        months = [f"{m:02d}" for m in range(1, 13)]
        days = [f"{d:02d}" for d in range(1, 32)]

        self.year_var = tk.StringVar(value=str(today.year))
        self.month_var = tk.StringVar(value=f"{today.month:02d}")
        self.day_var = tk.StringVar(value=f"{today.day:02d}")

        ctk.CTkLabel(form, text=t("Date début :")).grid(row=0, column=0,
                                                        sticky="w", pady=8)

        date_frame = ctk.CTkFrame(form, fg_color="transparent")
        date_frame.grid(row=0, column=1, padx=8, pady=8, sticky="w")

        ctk.CTkComboBox(date_frame, variable=self.year_var, values=years,
                        width=80, state="readonly").pack(side="left")
        ctk.CTkLabel(date_frame, text="-", width=10).pack(side="left")
        ctk.CTkComboBox(date_frame, variable=self.month_var, values=months,
                        width=65, state="readonly").pack(side="left")
        ctk.CTkLabel(date_frame, text="-", width=10).pack(side="left")
        ctk.CTkComboBox(date_frame, variable=self.day_var, values=days,
                        width=65, state="readonly").pack(side="left")

        ctk.CTkButton(date_frame, text=t("Aujourd'hui"), width=90, height=28,
                      fg_color="#2a2d35", hover_color="#353840",
                      font=ctk.CTkFont(size=11),
                      command=lambda: self._set_today(today)).pack(side="left", padx=(10, 0))

        self.reset_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(self,
                        text=t("Réinitialiser stock début = stock actuel "
                               "pour tous"),
                        variable=self.reset_var,
                        font=ctk.CTkFont(size=12)).pack(padx=30, pady=(8, 4), anchor="w")
        ctk.CTkLabel(self, text=t("Coche cette option pour démarrer une "
                                  "nouvelle période comptable"),
                     text_color="#6b7280", font=ctk.CTkFont(size=10)).pack(padx=48, anchor="w")

        self.status_lbl = ctk.CTkLabel(self, text="", text_color="orange")
        self.status_lbl.pack(pady=(8, 0))

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=10)
        ctk.CTkButton(btn_frame, text=t("Annuler"), width=100,
                      fg_color="#2a2d35", hover_color="#353840",
                      command=self.destroy).pack(side="left", padx=8)
        self.btn_appliquer = ctk.CTkButton(
            btn_frame, text=t("Appliquer à tous"), width=160,
            fg_color="#2563eb", hover_color="#1d4ed8", command=self._save)
        self.btn_appliquer.pack(side="left", padx=8)

    def _set_today(self, today):
        self.year_var.set(str(today.year))
        self.month_var.set(f"{today.month:02d}")
        self.day_var.set(f"{today.day:02d}")

    def _save(self):
        from datetime import date

        if self._envoi_en_cours:
            return
        try:
            y = int(self.year_var.get())
            m = int(self.month_var.get())
            d = int(self.day_var.get())
            chosen = date(y, m, d)
        except ValueError:
            self.status_lbl.configure(text=t("Date invalide"))
            return

        if chosen > date.today():
            self.status_lbl.configure(
                text=t("La date ne peut pas dépasser aujourd'hui"))
            return

        date_str = chosen.isoformat()
        reset = self.reset_var.get()

        # Les deux appels réseau passent par run_async, comme le reste du
        # fichier : appelés directement, ils gèlent la fenêtre le temps de la
        # requête et l'opérateur croit l'application plantée.
        if not reset:
            self._appliquer(date_str, reset=False)
            return

        self.status_lbl.configure(text=t("Vérification du catalogue…"))

        def _compte_ok(produits):
            self._confirmer_reset(date_str, len(produits or []))

        def _compte_echec(_e):
            # Le comptage n'est qu'un confort d'affichage : son échec ne doit
            # pas empêcher la confirmation.
            self._confirmer_reset(date_str, 0)

        run_async(self, self.api.get_products, _compte_ok, _compte_echec)

    def _confirmer_reset(self, date_str: str, nb: int):
        if not self.winfo_exists():
            return
        self.status_lbl.configure(text="")
        # Action irreversible sur tout le catalogue : on annonce le nombre
        # de produits touches avant d'ecraser les stocks de debut.
        detail = (t("{n} produit(s)").format(n=nb) if nb
                  else t("TOUS les produits"))
        if not messagebox.askyesno(
            t("Confirmer la réinitialisation"),
            t("Le stock de début de {detail} va être remplacé par le stock "
              "actuel, avec la date du {date}.\n\nCette action est "
              "IRRÉVERSIBLE : les anciens stocks de début seront "
              "définitivement perdus.\n\nContinuer ?").format(
                  detail=detail, date=date_str),
            icon="warning",
            parent=self,
        ):
            self.status_lbl.configure(text=t("Réinitialisation annulée"))
            return

        # Second verrou : recopier un mot. Un « Oui / Non » se clique par
        # réflexe — et un double-clic un peu vif sur « Appliquer à tous »
        # pouvait valider la première boîte avant même qu'elle soit lue.
        # C'est l'opération la plus destructrice du système : les anciens
        # stocks de début ne sont récupérables que dans une sauvegarde.
        if not demander_mot_de_confirmation(
            self,
            t("Dernière confirmation"),
            t("Réinitialisation du stock de début de {detail}, au {date}.\n\n"
              "Le serveur fera une sauvegarde de la base avant d'exécuter, "
              "mais l'opération elle-même ne s'annule pas.").format(
                  detail=detail, date=date_str),
            MOT_CONFIRMATION_RESET,
        ):
            self.status_lbl.configure(text=t("Réinitialisation annulée"))
            return
        self._appliquer(date_str, reset=True)

    def _appliquer(self, date_str: str, reset: bool):
        self.status_lbl.configure(text=t("Enregistrement…"))
        self._envoi_en_cours = True
        try:
            self.btn_appliquer.configure(state="disabled")
        except Exception:
            pass

        def _reactiver():
            self._envoi_en_cours = False
            try:
                self.btn_appliquer.configure(state="normal")
            except Exception:
                pass

        def _ok(result):
            if not self.winfo_exists():
                return
            count = (result or {}).get("updated", 0)
            archive = (result or {}).get("sauvegarde")
            messagebox.showinfo(
                t("Succès"),
                t("Date début mise à jour pour {n} produit(s).").format(n=count)
                + ("\n" + t("Stock début réinitialisé au stock actuel.")
                   if reset else "")
                + ("\n" + t("Sauvegarde de sécurité : {nom}").format(nom=archive)
                   if archive else ""),
                parent=self)
            if self.on_saved:
                self.on_saved()
            self.destroy()

        def _echec(e):
            if self.winfo_exists():
                _reactiver()
                self.status_lbl.configure(text=str(e))

        run_async(
            self,
            lambda: self.api.set_global_stock_date(date_str, reset_initial=reset),
            _ok, _echec)
