"""Inventaire régional : comptage déclaratif, purement informatif.

Aucun écart n'est calculé ici et AUCUN stock n'est ajusté : le comptage part
tel quel à l'entrepôt central, qui décide du réapprovisionnement. C'est un
choix délibéré — laisser une région corriger son propre stock ouvrirait la
porte à des ajustements sans contrôle.

La liste vient du catalogue complet (`/regional/catalogue`), pas du stock déjà
reçu par la région : une région qui n'a encore rien reçu d'un article doit
quand même pouvoir le compter (elle en a peut-être reçu hors système, ou veut
signaler qu'elle n'en a aucun), comme sur l'écran Produits du central.

Deux modes, comme sur l'écran Alertes : un compte régional SAISIT son comptage,
un admin le CONSULTE. L'admin ne compte jamais le stock physique d'une région à
sa place — lui montrer le formulaire de saisie n'avait aucun sens ; il lui faut
l'historique de ce que la région a déjà compté, avec la date.
"""
import tkinter as tk
import uuid
from datetime import date, datetime, timedelta
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk
from tkcalendar import DateEntry

from async_call import run_async
from file_attente import ajouter as mettre_en_attente
from i18n import t
from local_cache import charger_comptage, effacer_comptage, sauvegarder_comptage
from recherche import filtrer as filtrer_articles
from views.utils import float_saisie


class InventaireRegionalView(ctk.CTkFrame):

    def __init__(self, master, api, region: str | None = None,
                 user_role: str | None = None, sector: str | None = None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self.region = region or ""
        self.user_role = user_role or ""
        self.sector = sector
        # L'admin consulte, tout le reste saisit. Un rôle inconnu (session pas
        # encore chargée) est traité comme une région : c'est le mode d'un
        # compte régional connecté directement, et le serveur refuserait de
        # toute façon un envoi venant d'un compte sans région.
        self.mode_admin = self.user_role == "admin"
        self._articles: list[dict] = []
        self._saisies: dict[int, str] = {}
        # MULTI-01 : même patron que TransfertsRegionauxView._cle_idempotence.
        # None -> généré au premier envoi, remis à None une fois ce comptage
        # abouti (réseau ou file d'attente) pour que le SUIVANT en obtienne
        # une nouvelle.
        self._cle_idempotence: str | None = None
        # Reprise proposée une seule fois par instance de vue : sans ce
        # drapeau, chaque refresh() (bouton, F5, réouverture d'onglet)
        # reproposerait la question.
        self._reprise_proposee = False
        self._build()

    def _cle_brouillon(self) -> str:
        """Clé de fichier du brouillon, propre à CETTE région.

        Distincte de `CLE_COMPTAGE` ("general", inventory.py) : les deux
        écrans partagent le mécanisme `local_cache.sauvegarder_comptage`
        mais pas leur fichier — un poste régional multi-régions ne doit pas
        écraser le comptage d'une région avec celui d'une autre.
        """
        return f"regional_{self.region or 'sans_region'}"

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
            ctk.CTkButton(top, text=t("📤  Exporter"), width=130,
                          fg_color="#2a2d35", hover_color="#353840",
                          corner_radius=8, command=self._exporter).pack(
                side="right", padx=8)
        if not self.mode_admin:
            ctk.CTkButton(top, text=t("🕐  Comptages précédents"), width=190,
                          fg_color="#2a2d35", hover_color="#353840",
                          corner_radius=8, command=self._historique).pack(
                side="right", padx=8)

        if self.mode_admin:
            self._build_admin()
        else:
            self._build_region()

    # ---- vue administrateur : lecture seule des comptages déjà envoyés ----

    def _build_admin(self):
        ctk.CTkLabel(
            self,
            text=t("Comptages envoyés par la région — aucune saisie ici, "
                   "c'est la région qui compte son propre stock."),
            text_color="#6b7280", font=ctk.CTkFont(size=11),
            wraplength=900, justify="left").pack(anchor="w", padx=20, pady=(0, 6))

        self._build_filtre_dates()

        cadre = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                             border_width=1, border_color="#2a2d35")
        cadre.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        colonnes = ("date", "article", "quantite", "par")
        self.tree = ttk.Treeview(cadre, columns=colonnes, show="headings")
        for col, titre, largeur in (("date", t("Date du comptage"), 160),
                                    ("article", t("Article"), 300),
                                    ("quantite", t("Quantité comptée"), 130),
                                    ("par", t("Saisi par"), 160)):
            self.tree.heading(col, text=titre)
            self.tree.column(col, width=largeur, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        barre = ttk.Scrollbar(cadre, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=barre.set)
        barre.pack(side="right", fill="y")

        self._status = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11),
                                    text_color="#6b7280")
        self._status.pack(anchor="w", padx=20, pady=(0, 16))

    # ---- filtre de période (vue admin uniquement) ----

    def _build_filtre_dates(self):
        """Deux calendriers + « Appliquer », comme sur l'écran Historique.

        Même habillage et mêmes composants (tkcalendar) qu'ailleurs dans
        l'application : l'admin ne réapprend pas un widget par écran. La case
        à cocher porte l'état « aucun filtre », les calendriers affichant de
        toute façon toujours une date.
        """
        self._configurer_style_dates()

        barre = ctk.CTkFrame(self, fg_color="transparent")
        barre.pack(fill="x", padx=20, pady=(0, 6))

        self.date_filter_var = tk.BooleanVar(value=False)
        ctk.CTkCheckBox(barre, text=t("Filtrer par date"),
                        variable=self.date_filter_var,
                        font=ctk.CTkFont(size=11),
                        checkbox_width=18, checkbox_height=18,
                        fg_color="#2563eb", hover_color="#1d4ed8",
                        border_color="#2a2d35",
                        command=self.refresh).pack(side="left", padx=(0, 8))

        aujourdhui = date.today()
        debut_defaut = aujourdhui - timedelta(days=30)
        style_calendrier = dict(
            style="Dark.DateEntry", width=11, state="readonly",
            date_pattern="dd/mm/yyyy", firstweekday="monday",
            showweeknumbers=False,
            background="#1a1d23", foreground="#e5e7eb",
            bordercolor="#2a2d35",
            headersbackground="#1a1d23", headersforeground="#9ca3af",
            normalbackground="#1a1d23", normalforeground="#e5e7eb",
            weekendbackground="#22252c", weekendforeground="#e5e7eb",
            othermonthbackground="#15171c", othermonthforeground="#4b5563",
            othermonthwebackground="#15171c", othermonthweforeground="#4b5563",
            selectbackground="#2563eb", selectforeground="#ffffff",
        )

        ctk.CTkLabel(barre, text=t("Du"),
                     font=ctk.CTkFont(size=11)).pack(side="left")
        self.from_date = DateEntry(
            barre, year=debut_defaut.year, month=debut_defaut.month,
            day=debut_defaut.day, **style_calendrier)
        self.from_date.pack(side="left", padx=4)

        ctk.CTkLabel(barre, text=t("  Au"),
                     font=ctk.CTkFont(size=11)).pack(side="left")
        self.to_date = DateEntry(
            barre, year=aujourdhui.year, month=aujourdhui.month,
            day=aujourdhui.day, **style_calendrier)
        self.to_date.pack(side="left", padx=4)

        ctk.CTkButton(barre, text=t("Appliquer"), width=100, height=28,
                      corner_radius=6, fg_color="#2563eb",
                      hover_color="#1d4ed8", font=ctk.CTkFont(size=11),
                      command=self._appliquer_filtre_dates).pack(
            side="left", padx=(10, 0))
        ctk.CTkButton(barre, text=t("Tout"), width=70, height=28,
                      corner_radius=6, fg_color="#2a2d35",
                      hover_color="#353840", font=ctk.CTkFont(size=11),
                      command=self._effacer_filtre_dates).pack(
            side="left", padx=(6, 0))

    def _configurer_style_dates(self):
        """Habille les DateEntry aux couleurs sombres de l'application.

        Copie assumée de HistoryView : tkcalendar dérive son style de celui
        d'une Combobox ttk, et le style est nommé par arbre de widgets — le
        déclarer ici évite que cet écran dépende de l'ouverture préalable de
        l'historique pour être lisible.
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
        """(date_from, date_to) au format AAAA-MM-JJ, ou (None, None).

        (None, None) quand la case est décochée, ou quand l'écran n'a pas de
        filtre du tout (mode région) : l'appelant passe alors les deux bornes
        à None et le serveur ne filtre rien.
        """
        if not getattr(self, "date_filter_var", None) or not self.date_filter_var.get():
            return None, None
        try:
            debut = self.from_date.get_date()
            fin = self.to_date.get_date()
        except (ValueError, AttributeError):
            return None, None
        # Plage inversée : on l'ordonne plutôt que d'envoyer au serveur une
        # période qu'il refusera en 400, illisible pour un magasinier.
        if debut > fin:
            debut, fin = fin, debut
        return debut.strftime("%Y-%m-%d"), fin.strftime("%Y-%m-%d")

    def _appliquer_filtre_dates(self):
        """Le bouton « Appliquer » coche la case si elle ne l'est pas encore :
        cliquer sur un bouton de filtre sans rien voir changer déroute."""
        self.date_filter_var.set(True)
        self.refresh()

    def _effacer_filtre_dates(self):
        self.date_filter_var.set(False)
        self.refresh()

    # ---- vue régionale : le formulaire de comptage, inchangé ----

    def _build_region(self):
        ctk.CTkLabel(
            self,
            text=t("Saisis la quantité comptée pour chaque article. Ce "
                   "comptage est informatif : il ne modifie aucun stock, il "
                   "informe l'entrepôt central de ce qu'il te reste."),
            text_color="#6b7280", font=ctk.CTkFont(size=11),
            wraplength=900, justify="left").pack(anchor="w", padx=20, pady=(0, 6))

        entete = ctk.CTkFrame(self, fg_color="transparent")
        entete.pack(fill="x", padx=20, pady=(0, 4))
        ctk.CTkLabel(entete, text=t("Tous les articles"),
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color="#9ca3af").pack(side="left")
        self.recherche_var = tk.StringVar()
        self.recherche_var.trace_add("write", lambda *_: self._peupler_liste())
        ctk.CTkEntry(entete, textvariable=self.recherche_var, width=220,
                     placeholder_text=t("Rechercher un article…")).pack(side="right")

        self._liste = ctk.CTkScrollableFrame(
            self, fg_color="#1a1d23", corner_radius=10)
        self._liste.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        ctk.CTkLabel(self, text=t("Commentaire (facultatif)"), text_color="#9ca3af",
                     font=ctk.CTkFont(size=11)).pack(anchor="w", padx=22)
        self.note_var = tk.StringVar()
        ctk.CTkEntry(self, textvariable=self.note_var,
                     placeholder_text=t("Ex. : comptage de fin de mois")).pack(
            fill="x", padx=20, pady=(2, 6))

        bas = ctk.CTkFrame(self, fg_color="transparent")
        bas.pack(fill="x", padx=20, pady=(0, 16))
        self._status = ctk.CTkLabel(bas, text="", font=ctk.CTkFont(size=11),
                                    text_color="#6b7280")
        self._status.pack(side="left")
        self._bouton = ctk.CTkButton(bas, text=t("📤  Envoyer le comptage"),
                                     width=190,
                                     fg_color="#2563eb", hover_color="#1d4ed8",
                                     command=self._envoyer)
        self._bouton.pack(side="right")

    def _libelle_titre(self) -> str:
        if self.mode_admin:
            if self.region:
                return t("📋  Comptages reçus — {region}").format(
                    region=self.region)
            return t("📋  Comptages reçus des régions")
        if self.region:
            return t("📋  Inventaire — {region}").format(region=self.region)
        return t("📋  Inventaire régional")

    def definir_region(self, region: str | None):
        self.region = region or ""
        self._titre.configure(text=self._libelle_titre())

    # ------------------------------------------------------------- données

    def refresh(self):
        self._status.configure(text=t("Chargement…"), text_color="#6b7280")
        if self.mode_admin:
            date_from, date_to = self._plage_dates()
            run_async(self,
                      lambda: self.api.get_inventaires_regionaux(
                          self.region or None, date_from=date_from,
                          date_to=date_to),
                      self._afficher_admin, self._erreur)
        else:
            run_async(self, self.api.get_catalogue_regional,
                      self._afficher, self._erreur)

    def _erreur(self, exc):
        self._status.configure(text=str(exc), text_color="orange")

    # ------------------------------------------------------- affichage admin

    def _afficher_admin(self, data):
        rapports = data.get("rapports", [])
        self.tree.delete(*self.tree.get_children())
        for r in rapports:
            self.tree.insert("", "end", values=(
                (r.get("created_at") or "")[:16],
                f"{r.get('name', '')} · {r.get('sku', '')}",
                f"{r.get('quantity', 0):g} {r.get('unit') or ''}".strip(),
                r.get("created_by") or "—",
            ))
        date_from, date_to = self._plage_dates()
        periode = ""
        if date_from or date_to:
            periode = t(" (du {debut} au {fin})").format(
                debut=date_from or "…", fin=date_to or "…")
        if rapports:
            self._status.configure(
                text=t("{n} ligne(s) comptée(s){periode} — le comptage le plus "
                       "récent est en haut").format(n=len(rapports),
                                                    periode=periode),
                text_color="#6b7280")
        elif periode:
            # Distinguer « rien sur cette période » de « rien du tout » :
            # sinon l'admin croit que la région n'a jamais rien envoyé alors
            # qu'il regarde simplement le mauvais mois.
            self._status.configure(
                text=t("Aucun comptage sur cette période{periode}. "
                       "Élargis les dates ou clique sur « Tout ».").format(
                           periode=periode),
                text_color="#6b7280")
        else:
            self._status.configure(text=t("Aucun comptage envoyé pour l'instant."),
                                   text_color="#6b7280")

    def _exporter(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")],
            initialfile=f"inventaire_{self.region or 'region'}.xlsx")
        if not path:
            return
        # L'export porte la MÊME période que la liste affichée : un fichier
        # qui ne correspond pas à ce qu'on a sous les yeux est pire qu'une
        # absence d'export.
        date_from, date_to = self._plage_dates()

        def _ok(_res):
            messagebox.showinfo(t("Export"),
                                t("Comptages exportés vers {path}").format(path=path))

        def _err(exc):
            messagebox.showerror(t("Erreur"), str(exc))

        run_async(self,
                  lambda: self.api.export_inventaires_regionaux(
                      path, self.region or None, date_from=date_from,
                      date_to=date_to),
                  _ok, _err)

    # ------------------------------------------------------ affichage région

    def _afficher(self, data):
        self._articles = data.get("produits", [])
        # Reprend les StringVar déjà en place plutôt que d'en recréer, quand
        # l'id existe encore : `refresh()` est appelé bien au-delà du premier
        # chargement — bouton « Rafraîchir », réouverture de l'onglet
        # (main.py) et surtout F5 (raccourci global) le déclenchent SANS
        # confirmation. Sans cette reprise, un comptage en cours sur un
        # catalogue de plusieurs dizaines d'articles (poste régional, liaison
        # lente) était perdu en silence au moindre F5 — repéré par audit le
        # 9 septembre 2026. Sans risque après un envoi réussi : `_envoyer`
        # vide déjà chaque StringVar à "" avant d'appeler `refresh()`, donc
        # rien d'ancien ne peut « fuiter » dans le comptage suivant.
        # `tk.StringVar()` en valeur par défaut de `dict.get` serait évaluée
        # à CHAQUE itération, y compris quand la clé existe déjà (Python
        # évalue les deux arguments avant l'appel) : une StringVar neuve et
        # aussitôt jetée par produit du catalogue, à chaque refresh. D'où le
        # if/else explicite plutôt qu'un `.get(id, tk.StringVar())`.
        anciennes_saisies = self._saisies
        premiere_fois = not anciennes_saisies and not self._reprise_proposee
        self._saisies = {
            a["id"]: (anciennes_saisies[a["id"]] if a["id"] in anciennes_saisies
                       else tk.StringVar())
            for a in self._articles
        }
        self._peupler_liste()
        # Reprise d'un comptage interrompu par une DESTRUCTION de la vue
        # (session expirée, désactivation de compte, changement de mot de
        # passe pendant la saisie — `main.py` recrée toutes les vues à la
        # reconnexion) : la reprise de StringVar ci-dessus ne protège que
        # les refresh() d'une même instance encore vivante, pas ce cas-là.
        # Contrairement à l'écran Inventaire (inventory.py), cet écran
        # n'avait ABSOLUMENT aucune persistance — repéré par audit de
        # robustesse le 9 septembre 2026. Proposée une seule fois par
        # instance (`_reprise_proposee`), seulement au tout premier
        # peuplement de cette vue (jamais après un envoi réussi, qui vide
        # déjà les StringVar avant de rappeler refresh()).
        if premiere_fois:
            self._reprise_proposee = True
            self._proposer_reprise_brouillon()

    def _proposer_reprise_brouillon(self) -> None:
        brouillon = charger_comptage(self._cle_brouillon(), sector=self.sector)
        if not brouillon:
            return
        comptages = brouillon.get("comptages") or {}
        if not comptages:
            return
        auteur = (brouillon.get("username") or "").strip()
        quand = (brouillon.get("created_at") or "").replace("T", " ")
        detail = t("Commencé le {quand}").format(quand=quand) if quand else ""
        if auteur:
            detail += t(" par {qui}").format(qui=auteur)
        if not messagebox.askyesno(
            t("Comptage interrompu"),
            t("Un comptage régional n'a pas été terminé : {n} quantité(s) "
              "déjà saisies.\n{detail}\n\nReprendre ce comptage ?\n\n"
              "« Non » efface ces saisies et repart d'un comptage vide.").format(
                  n=len(comptages), detail=detail),
        ):
            effacer_comptage(self._cle_brouillon())
            return
        restaures = 0
        for pid, var in self._saisies.items():
            valeur = comptages.get(str(pid))
            if valeur is None:
                continue
            var.set(str(valeur))
            restaures += 1
        self._status.configure(
            text=t("Comptage repris : {n} quantité(s) restaurée(s).").format(
                n=restaures),
            text_color="#6b7280")

    def _sauver_brouillon(self, *_args) -> None:
        """Écrit le comptage en cours sur le disque du poste.

        Appelée à chaque `<FocusOut>` d'un champ de quantité plutôt qu'à
        chaque frappe (`trace_add("write", ...)` sur ~200 StringVar
        écrirait le disque à chaque caractère tapé, sur ce poste comme sur
        tous les autres — `<FocusOut>` capture la même intention (« j'ai
        fini de saisir cette ligne ») sans ce coût.
        """
        comptages = {str(pid): var.get() for pid, var in self._saisies.items()
                     if var.get().strip()}
        if not comptages:
            effacer_comptage(self._cle_brouillon())
            return
        sauvegarder_comptage(self._cle_brouillon(),
                             {"comptages": comptages, "region": self.region},
                             sector=self.sector)

    def _peupler_liste(self):
        for enfant in self._liste.winfo_children():
            enfant.destroy()

        # Tolérante aux fautes de frappe (voir recherche.py) : « filre » trouve
        # « Filtre ». Le code reste littéral, seul le nom pardonne.
        filtre = (self.recherche_var.get() or "").strip()
        visibles = filtrer_articles(self._articles, filtre)

        if not self._articles:
            ctk.CTkLabel(self._liste, text=t("Aucun produit dans le catalogue."),
                         text_color="#6b7280").pack(anchor="w", padx=12, pady=12)
            self._status.configure(text=t("Rien à compter"), text_color="#6b7280")
            return
        if not visibles:
            ctk.CTkLabel(self._liste,
                         text=t("Aucun article ne correspond à ta recherche."),
                         text_color="#6b7280").pack(anchor="w", padx=12, pady=12)
            self._status.configure(
                text=t("{n} article(s) au catalogue").format(n=len(self._articles)),
                                   text_color="#6b7280")
            return

        # Pas de « stock théorique » affiché, délibérément : le montrer pousse
        # à recopier le chiffre attendu au lieu de compter ce qu'on a devant
        # soi. Le comptage doit être une observation, pas une confirmation.
        for col, texte in enumerate(("Article", "Unité", "Compté")):
            ctk.CTkLabel(self._liste, text=t(texte),
                         font=ctk.CTkFont(size=11, weight="bold"),
                         text_color="#9ca3af").grid(row=0, column=col, padx=8,
                                                    pady=(6, 4), sticky="w")

        for i, article in enumerate(visibles, start=1):
            pid = article["id"]
            ctk.CTkLabel(self._liste,
                         text=f"{article['name']} · {article['sku']}",
                         text_color="#d1d5db", anchor="w").grid(
                row=i, column=0, padx=8, pady=3, sticky="w")
            ctk.CTkLabel(self._liste, text=article.get("unit") or "",
                         text_color="#6b7280").grid(row=i, column=1, padx=8, pady=3)

            champ = ctk.CTkEntry(self._liste, textvariable=self._saisies[pid], width=100,
                                 placeholder_text="—")
            champ.grid(row=i, column=2, padx=8, pady=3)
            # <FocusOut>, pas trace_add("write", ...) : sur ~200 champs, une
            # écriture disque à chaque caractère tapé serait un coût pour
            # rien — <FocusOut> capture la même intention (« j'ai fini de
            # saisir cette ligne ») sans ce coût.
            champ.bind("<FocusOut>", self._sauver_brouillon)

        self._status.configure(
            text=t("{n} article(s) au catalogue").format(n=len(self._articles)),
            text_color="#6b7280")

    # ------------------------------------------------------------- actions

    def _lignes_saisies(self) -> tuple[list[dict], str | None]:
        """Renvoie (lignes, erreur). Une case vide n'est pas comptée."""
        lignes = []
        for article in self._articles:
            pid = article["id"]
            texte = self._saisies[pid].get().strip()
            if not texte:
                continue
            try:
                valeur = float_saisie(texte)
            except ValueError:
                return [], t("Quantité invalide pour « {nom} »").format(
                    nom=article["name"])
            if valeur < 0:
                return [], t("Quantité négative pour « {nom} »").format(
                    nom=article["name"])
            lignes.append({"product_id": pid, "quantity": valeur})
        return lignes, None

    def _envoyer(self):
        lignes, erreur = self._lignes_saisies()
        if erreur:
            self._status.configure(text=erreur, text_color="orange")
            return
        if not lignes:
            self._status.configure(text=t("Saisis au moins une quantité comptée"),
                                   text_color="orange")
            return

        # Garde `if None` : un ré-essai du même envoi (retry après erreur
        # réseau, avant mise en file) réutilise la clé plutôt que d'en générer
        # une nouvelle — sinon deux tentatives du MÊME comptage se
        # présenteraient au serveur comme deux comptages différents.
        if self._cle_idempotence is None:
            self._cle_idempotence = str(uuid.uuid4())

        self._bouton.configure(state="disabled", text=t("Envoi…"))

        # Posée AVANT l'envoi, pas après : c'est la date du comptage physique.
        # Si l'envoi échoue et part en file d'attente, le rejeu (peut-être
        # des jours plus tard, à la reprise du réseau) doit rester daté du
        # jour où le magasinier a réellement compté, pas du jour de la reprise.
        horodatage = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload_envoi = {
            "lines": lignes, "region": self.region or None,
            "note": self.note_var.get().strip(), "created_at": horodatage,
            # Part AVEC le comptage, y compris en file d'attente : c'est elle
            # qui empêche un rejeu de créer un second rapport et de doubler
            # silencieusement le total agrégé du dernier comptage.
            "idempotency_key": self._cle_idempotence,
        }

        def _ok(resultat):
            if self.winfo_exists():
                self._bouton.configure(state="normal",
                                       text=t("📤  Envoyer le comptage"))
            messagebox.showinfo(
                t("Comptage envoyé"),
                t("{n} ligne(s) transmises à l'entrepôt central.\n\nAucun "
                  "stock n'a été modifié : ce comptage est informatif.").format(
                      n=resultat.get("lignes", len(lignes))))
            self.note_var.set("")
            # Comptage abouti : le prochain envoi est un autre comptage, il
            # lui faut sa propre clé.
            self._cle_idempotence = None
            # Vide aussi les quantités saisies tout de suite, sans attendre le
            # retour asynchrone de refresh() : entre les deux, l'écran
            # affichait encore les anciennes valeurs avec le bouton réactivé,
            # et un renvoi manuel pendant cette fenêtre créait un second
            # comptage volontaire du même contenu (couvert par la clé s'il
            # est rejoué, mais pas s'il est resaisi à la main).
            for var in self._saisies.values():
                var.set("")
            effacer_comptage(self._cle_brouillon())
            self.refresh()

        def _err(exc):
            if not self.winfo_exists():
                return
            self._bouton.configure(state="normal", text=t("📤  Envoyer le comptage"))
            erreur = str(exc)
            if "contacter le serveur" in erreur.lower() or "timeout" in erreur.lower():
                if messagebox.askyesno(
                    t("Serveur injoignable"),
                    t("Le serveur ne répond pas.\n\nMettre ce comptage en file "
                      "d'attente ?\nIl sera envoyé automatiquement au retour du "
                      "réseau, avec la date d'aujourd'hui — pas celle de l'envoi.")):
                    n = mettre_en_attente("regional_inventory", payload_envoi,
                                          username=self.api.username)
                    messagebox.showinfo(
                        t("En file d'attente"),
                        t("Comptage mis en attente ({n} en file).\nIl sera "
                          "envoyé dès que le serveur sera joignable.").format(n=n))
                    # La clé est partie avec le payload mis en file : elle a
                    # fait son travail (le rejeu la réutilisera), le prochain
                    # envoi depuis cet écran aura la sienne.
                    self._cle_idempotence = None
                    self.note_var.set("")
                    for var in self._saisies.values():
                        var.set("")
                    effacer_comptage(self._cle_brouillon())
                    return
            self._status.configure(text=erreur, text_color="orange")

        run_async(self,
                  lambda: self.api.creer_inventaire_regional(**payload_envoi),
                  _ok, _err)

    def _historique(self):
        self._status.configure(text=t("Chargement de l'historique…"),
                               text_color="#6b7280")

        def _ok(data):
            self._status.configure(text="", text_color="#6b7280")
            HistoriqueInventaireDialog(self, data.get("rapports", []))

        run_async(self,
                  lambda: self.api.get_inventaires_regionaux(self.region or None),
                  _ok, self._erreur)


class HistoriqueInventaireDialog(ctk.CTkToplevel):
    def __init__(self, master, rapports: list[dict]):
        super().__init__(master)
        self.title(t("Comptages précédents"))
        self.geometry("640x420")
        self.transient(master)
        self.lift()
        self.grab_set()

        ctk.CTkLabel(self, text=t("Comptages déjà envoyés"),
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(12, 8))

        cadre = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                             border_width=1, border_color="#2a2d35")
        cadre.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        colonnes = ("date", "article", "quantite", "par")
        tree = ttk.Treeview(cadre, columns=colonnes, show="headings", height=15)
        for col, titre, largeur in (("date", t("Date"), 150),
                                    ("article", t("Article"), 260),
                                    ("quantite", t("Compté"), 90),
                                    ("par", t("Saisi par"), 140)):
            tree.heading(col, text=titre)
            tree.column(col, width=largeur, anchor="w")
        tree.pack(side="left", fill="both", expand=True)
        barre = ttk.Scrollbar(cadre, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=barre.set)
        barre.pack(side="right", fill="y")

        for r in rapports:
            tree.insert("", "end", values=(
                (r.get("created_at") or "")[:16],
                f"{r.get('name', '')} · {r.get('sku', '')}",
                f"{r.get('quantity', 0):g} {r.get('unit') or ''}".strip(),
                r.get("created_by") or "—",
            ))

        ctk.CTkButton(self, text=t("Fermer"), width=100, fg_color="gray30",
                      command=self.destroy).pack(pady=8)
