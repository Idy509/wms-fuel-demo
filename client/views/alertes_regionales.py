"""Alertes régionales : signalements manuels de rupture.

Pas de calcul automatique par seuil, délibérément. Le stock d'un entrepôt
régional ne baisse pas quand les techniciens se servent — rien n'est saisi —
il ne bouge qu'à la réception ou au retour vers le central. Comparer ce stock
au `min_stock` du produit ne déclenchait donc jamais, ou déclenchait à tort.
C'est la région qui dit « je n'ai plus de X », le central qui confirme avoir
réapprovisionné.

Le même écran sert aux deux : un compte régional signale, un admin résout.
"""
import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk

from async_call import run_async
from i18n import t
from recherche import filtrer as filtrer_articles


class AlertesRegionalesView(ctk.CTkFrame):

    def __init__(self, master, api, region: str | None = None,
                 user_role: str | None = None, on_signal_change=None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self.region = region or ""
        self.user_role = user_role or ""
        # Notifie le badge nav (toutes régions confondues) après une
        # résolution : lui seul sait recompter, cet écran n'affiche qu'UNE
        # région à la fois.
        self.on_signal_change = on_signal_change
        # L'admin résout, tout le reste signale. Un rôle inconnu (session pas
        # encore chargée) est traité comme une région : signaler ne casse rien,
        # résoudre par erreur ferait disparaître une demande réelle.
        self.mode_admin = self.user_role == "admin"
        self._signalements: list[dict] = []
        self._articles: list[dict] = []
        self._deja_signales: set[int] = set()
        self._build()

    # ------------------------------------------------------------ interface

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 8))
        self._titre = ctk.CTkLabel(
            top, text=self._libelle_titre(),
            font=ctk.CTkFont(size=20, weight="bold"), text_color="#f0f0f0")
        self._titre.pack(side="left")
        ctk.CTkButton(top, text=t("↻  Rafraîchir"), width=110,
                      fg_color="#2a2d35", hover_color="#353840",
                      corner_radius=8, command=self.refresh).pack(side="right", padx=8)

        if self.mode_admin:
            self._build_admin()
        else:
            self._build_region()

        self._status = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=12),
                                    text_color="#6b7280")
        self._status.pack(anchor="w", padx=20, pady=(0, 16))

    # ---- vue administrateur : la liste des demandes ouvertes, à résoudre ----

    def _build_admin(self):
        ctk.CTkLabel(
            self,
            text=t("Ruptures signalées par les régions, toutes confondues, "
                   "pas encore traitées. Marque un signalement résolu une "
                   "fois le réapprovisionnement expédié."),
            text_color="#6b7280", font=ctk.CTkFont(size=11),
            wraplength=900, justify="left").pack(anchor="w", padx=20, pady=(0, 6))

        cadre = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                             border_width=1, border_color="#2a2d35")
        cadre.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        # Toutes les régions dans la même liste, avec une colonne « Région » :
        # forcer l'admin à sélectionner une région pour découvrir où est le
        # problème revenait à chercher une aiguille dans une botte de foin.
        colonnes = ("region", "sku", "article", "unite", "note", "par", "date")
        self.tree = ttk.Treeview(cadre, columns=colonnes, show="headings")
        entetes = {"region": t("Région"), "sku": t("Code"),
                   "article": t("Article"), "unite": t("Unité"),
                   "note": t("Précision"), "par": t("Signalé par"),
                   "date": t("Le")}
        largeurs = {"region": 130, "sku": 100, "article": 230, "unite": 60,
                    "note": 200, "par": 130, "date": 120}
        for col in colonnes:
            self.tree.heading(col, text=entetes[col])
            self.tree.column(col, width=largeurs[col], anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        barre = ttk.Scrollbar(cadre, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=barre.set)
        barre.pack(side="right", fill="y")
        self.tree.bind("<Double-1>", lambda _e: self._resoudre_selection())

        bas = ctk.CTkFrame(self, fg_color="transparent")
        bas.pack(fill="x", padx=20, pady=(0, 4))
        ctk.CTkButton(bas, text=t("✓  Marquer résolu"), width=180,
                      fg_color="#16a34a", hover_color="#15803d",
                      command=self._resoudre_selection).pack(side="right")

    # ---- vue régionale : signaler en un geste, voir ce qui est déjà parti ---

    def _build_region(self):
        ctk.CTkLabel(
            self,
            text=t("Signale à l'entrepôt central un article dont tu manques, "
                   "sans attendre l'inventaire. Un clic sur « Signaler » "
                   "suffit ; la précision est facultative."),
            text_color="#6b7280", font=ctk.CTkFont(size=11),
            wraplength=900, justify="left").pack(anchor="w", padx=20, pady=(0, 6))

        # Déjà signalé : posé en haut pour qu'on le voie AVANT de re-signaler.
        ctk.CTkLabel(self, text=t("Déjà signalé, en attente du central"),
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color="#f59e0b").pack(anchor="w", padx=20, pady=(4, 2))
        self._cadre_signales = ctk.CTkScrollableFrame(
            self, fg_color="#1a1d23", corner_radius=10, height=110)
        self._cadre_signales.pack(fill="x", padx=20, pady=(0, 8))

        entete = ctk.CTkFrame(self, fg_color="transparent")
        entete.pack(fill="x", padx=20, pady=(4, 2))
        ctk.CTkLabel(entete, text=t("Tous les articles"),
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color="#9ca3af").pack(side="left")
        self.recherche_var = tk.StringVar()
        self.recherche_var.trace_add("write", lambda *_: self._peupler_articles())
        ctk.CTkEntry(entete, textvariable=self.recherche_var, width=220,
                     placeholder_text=t("Rechercher un article…")).pack(side="right")

        self._cadre_articles = ctk.CTkScrollableFrame(
            self, fg_color="#1a1d23", corner_radius=10)
        self._cadre_articles.pack(fill="both", expand=True, padx=20, pady=(0, 8))

    def _libelle_titre(self) -> str:
        if self.mode_admin:
            # Toutes les régions dans la même liste : un titre par région
            # n'aurait plus de sens ici (voir refresh()/_afficher_admin).
            return t("⚠  Alertes régionales — toutes régions")
        if self.region:
            return t("⚠  Alertes — {region}").format(region=self.region)
        return t("⚠  Alertes régionales")

    def definir_region(self, region: str | None):
        self.region = region or ""
        self._titre.configure(text=self._libelle_titre())

    # ------------------------------------------------------------- données

    def refresh(self):
        self._status.configure(text=t("Chargement…"), text_color="#6b7280")
        if self.mode_admin:
            # Toutes régions confondues, jamais filtré par self.region : le
            # sélecteur de région en haut de l'écran ne sert plus qu'aux
            # onglets Réception/Inventaire, pas à celui-ci.
            run_async(self, self.api.get_toutes_alertes_regionales,
                      self._afficher_admin, self._erreur)
        else:
            run_async(self, self._charger_region,
                      self._afficher_region, self._erreur)

    def _charger_region(self) -> dict:
        """Un seul aller-retour logique : les signalements et le catalogue.

        Le catalogue complet, pas le stock régional — une région peut manquer
        d'un article qu'elle n'a jamais reçu, et doit pouvoir le signaler
        quand même plutôt que d'être limitée à ce qu'elle a déjà en stock.
        `/regional/catalogue` (pas `/products`, fermé à ce rôle : coût unitaire
        et stock central ne regardent pas la région).
        """
        alertes = self.api.get_alertes_regionales(self.region or None)
        catalogue = self.api.get_catalogue_regional()
        return {"alertes": alertes, "produits": catalogue.get("produits", [])}

    def _erreur(self, exc):
        self._status.configure(text=str(exc), text_color="orange")

    # ------------------------------------------------------- affichage admin

    def _afficher_admin(self, data):
        self._signalements = data.get("alertes", [])
        self.tree.delete(*self.tree.get_children())
        for a in self._signalements:
            self.tree.insert("", "end", iid=str(a["signal_id"]), values=(
                a.get("region", ""), a.get("sku", ""), a.get("name", ""),
                a.get("unit") or "", a.get("note") or "—",
                a.get("created_by") or "—", (a.get("created_at") or "")[:16],
            ))
        if self._signalements:
            self._status.configure(
                text=t("⚠  {n} rupture(s) signalée(s) et non traitée(s)").format(
                    n=len(self._signalements)),
                text_color="#f59e0b")
        else:
            self._status.configure(text=t("✓  Aucune rupture signalée"),
                                   text_color="#22c55e")

    def _resoudre_selection(self):
        selection = self.tree.selection()
        if not selection:
            self._status.configure(text=t("Sélectionne d'abord un signalement"),
                                   text_color="orange")
            return
        signal_id = int(selection[0])
        valeurs = self.tree.item(selection[0], "values")
        region_ligne, nom = valeurs[0], valeurs[2]
        if not messagebox.askyesno(
                t("Marquer résolu"),
                t("Confirmer que « {nom} » a été réapprovisionné pour "
                  "{region} ?\n\nLe signalement disparaîtra de la liste.").format(
                      nom=nom, region=region_ligne)):
            return

        def _ok(_res):
            self.refresh()
            if self.on_signal_change:
                self.on_signal_change()

        run_async(self, lambda: self.api.resoudre_alerte_regionale(signal_id),
                  _ok, self._erreur)

    # ------------------------------------------------------ affichage région

    def _afficher_region(self, data):
        alertes = data["alertes"]
        self.definir_region(alertes.get("region") or self.region)
        self._signalements = alertes.get("alertes", [])
        self._deja_signales = {s["product_id"] for s in self._signalements}
        self._articles = data["produits"]
        self._peupler_signales()
        self._peupler_articles()

        if self._signalements:
            self._status.configure(
                text=t("{n} signalement(s) en attente du central").format(
                    n=len(self._signalements)),
                text_color="#f59e0b")
        else:
            self._status.configure(text=t("Aucun signalement en cours"),
                                   text_color="#6b7280")

    def _peupler_signales(self):
        for enfant in self._cadre_signales.winfo_children():
            enfant.destroy()
        if not self._signalements:
            ctk.CTkLabel(self._cadre_signales,
                         text=t("Rien de signalé pour l'instant."),
                         text_color="#6b7280").pack(anchor="w", padx=12, pady=8)
            return
        for s in self._signalements:
            ligne = ctk.CTkFrame(self._cadre_signales, fg_color="transparent")
            ligne.pack(fill="x", padx=8, pady=2)
            texte = f"⚠  {s.get('name', '')} · {s.get('sku', '')}"
            if s.get("note"):
                texte += f" — {s['note']}"
            ctk.CTkLabel(ligne, text=texte, text_color="#fbbf24",
                         anchor="w").pack(side="left")
            ctk.CTkLabel(ligne, text=(s.get("created_at") or "")[:16],
                         text_color="#6b7280",
                         font=ctk.CTkFont(size=10)).pack(side="right")

    def _peupler_articles(self):
        for enfant in self._cadre_articles.winfo_children():
            enfant.destroy()

        # Même recherche tolérante que l'inventaire régional : le magasinier
        # qui manque d'un article doit pouvoir le signaler même s'il en écorche
        # le nom.
        filtre = (self.recherche_var.get() or "").strip()
        visibles = filtrer_articles(self._articles, filtre)

        if not visibles:
            message = (t("Aucun article ne correspond à ta recherche.")
                       if filtre else t("Aucun article dans le catalogue."))
            ctk.CTkLabel(self._cadre_articles, text=message,
                         text_color="#6b7280").pack(anchor="w", padx=12, pady=12)
            return

        for article in visibles:
            pid = article["id"]
            ligne = ctk.CTkFrame(self._cadre_articles, fg_color="transparent")
            ligne.pack(fill="x", padx=8, pady=2)
            ctk.CTkLabel(ligne,
                         text=f"{article.get('name', '')} · {article.get('sku', '')}",
                         text_color="#d1d5db", anchor="w").pack(side="left")
            if pid in self._deja_signales:
                ctk.CTkLabel(ligne, text=t("déjà signalé"), text_color="#f59e0b",
                             font=ctk.CTkFont(size=11)).pack(side="right", padx=6)
            else:
                ctk.CTkButton(
                    ligne, text=t("⚠  Signaler"), width=110, height=26,
                    fg_color="#b45309", hover_color="#92400e",
                    font=ctk.CTkFont(size=11),
                    command=lambda a=article: self._signaler(a)).pack(
                        side="right", padx=6)

    def _signaler(self, article: dict):
        SignalementDialog(self, article, self._envoyer_signalement)

    def _envoyer_signalement(self, article: dict, note: str):
        self._status.configure(text=t("Envoi du signalement…"),
                               text_color="#6b7280")

        def _ok(res):
            messagebox.showinfo(
                t("Signalé"),
                res.get("message")
                or t("« {nom} » signalé à l'entrepôt central.").format(
                    nom=article.get("name", "")))
            self.refresh()

        def _err(exc):
            # Le doublon est refusé par le serveur (409) : on le dit sans
            # drame, la demande précédente est toujours ouverte.
            messagebox.showwarning(t("Signalement impossible"), str(exc))
            self.refresh()

        run_async(self,
                  lambda: self.api.signaler_alerte_regionale(
                      article["id"], note, self.region or None),
                  _ok, _err)


class SignalementDialog(ctk.CTkToplevel):
    """Confirmation courte : le geste doit tenir en quelques secondes.

    La précision est facultative et Entrée valide : un magasinier pressé
    clique, appuie sur Entrée, c'est parti.
    """

    def __init__(self, master, article: dict, on_valider):
        super().__init__(master)
        self.title(t("Signaler une rupture"))
        self.geometry("460x230")
        self.resizable(False, False)
        self.transient(master)
        self.lift()
        self.grab_set()
        self._article = article
        self._on_valider = on_valider

        ctk.CTkLabel(self, text=t("Signaler à l'entrepôt central"),
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(16, 4))
        ctk.CTkLabel(self,
                     text=f"{article.get('name', '')} · {article.get('sku', '')}",
                     text_color="#fbbf24",
                     font=ctk.CTkFont(size=13)).pack(pady=(0, 10))

        ctk.CTkLabel(self, text=t("Précision (facultative)"), text_color="#9ca3af",
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=20)
        self.note_var = tk.StringVar()
        entree = ctk.CTkEntry(self, textvariable=self.note_var,
                              placeholder_text=t("Ex. : plus rien depuis lundi"))
        entree.pack(fill="x", padx=20, pady=(2, 12))
        entree.bind("<Return>", lambda _e: self._valider())
        entree.focus_set()

        boutons = ctk.CTkFrame(self, fg_color="transparent")
        boutons.pack(pady=(0, 12))
        ctk.CTkButton(boutons, text=t("Annuler"), width=110, fg_color="gray30",
                      command=self.destroy).pack(side="left", padx=6)
        ctk.CTkButton(boutons, text=t("⚠  Signaler"), width=140,
                      fg_color="#b45309", hover_color="#92400e",
                      command=self._valider).pack(side="left", padx=6)

    def _valider(self):
        note = self.note_var.get().strip()
        self.destroy()
        self._on_valider(self._article, note)
