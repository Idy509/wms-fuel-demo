"""Transfert d'entrepôt régional à entrepôt régional.

Jusqu'ici, faire passer un article d'une région à une autre obligeait à le
renvoyer au central puis à le réexpédier : deux bons, deux confirmations, et
un trajet que le camion n'a jamais fait. Cet écran permet à une région
d'envoyer directement à une région voisine.

Même transit en deux temps que le transfert venu du central : la marchandise
quitte l'entrepôt d'origine dès l'envoi, et n'est créditée à la région
destinataire qu'une fois qu'elle a confirmé sa réception (écran « Réception
régionale », colonne « Provenance »).

Le menu « Article » liste TOUT le catalogue actif, avec la quantité détenue
par la région en face — « 0 gal disponible(s) » pour ce qu'elle n'a pas.
N'y montrer que les articles détenus laissait le magasinier chercher un
article tout simplement absent de la liste, sans jamais lui dire pourquoi ;
avec le zéro affiché, la réponse est sous ses yeux.

Si le serveur est injoignable à l'envoi, le transfert part en file d'attente
hors ligne (`file_attente`, type « regional_transfer ») plutôt que d'être
perdu : le camion part quand il doit partir, le bon suit au retour du réseau.
Le plafond de stock est alors vérifié par le serveur au REJEU, pas à la
saisie — d'où l'avertissement affiché à l'opérateur.

Le plafond, lui, ne bouge pas : `_ajouter_ligne` refuse toujours de mettre au
panier plus que ce que la région détient réellement. C'est le même plafond
que celui appliqué par le serveur, dit avant la saisie plutôt qu'après le
refus — une contrainte métier, pas une limite d'affichage.
"""
import tkinter as tk
import uuid
from datetime import datetime
from tkinter import messagebox, ttk

import customtkinter as ctk

from async_call import run_async
from file_attente import ajouter as mettre_en_attente
from i18n import t
from local_cache import charger_brouillon, effacer_brouillon, sauvegarder_brouillon
from views.utils import float_saisie

FOND = "#1a1d23"
BORDURE = "#2a2d35"
GRIS = "#6b7280"
GRIS_CLAIR = "#9ca3af"
VERT = "#4ade80"
ORANGE = "#f59e0b"


class TransfertsRegionauxView(ctk.CTkFrame):

    def __init__(self, master, api, region: str | None = None,
                 user_role: str | None = None, regions_possibles=None,
                 sector: str | None = None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self.region = region or ""
        self.user_role = user_role or ""
        self.sector = sector
        # Un admin consulte : il n'expédie pas à la place d'une région, comme
        # il ne compte ni ne confirme à sa place. Le serveur accepterait son
        # envoi (il n'est pas lecteur), mais la trace porterait son nom alors
        # que personne n'aurait chargé le camion.
        self.mode_admin = self.user_role == "admin"
        self._regions_possibles = list(regions_possibles or [])
        self._stock: list[dict] = []
        self._panier: dict[int, float] = {}
        self._sortants: list[dict] = []
        # Clé d'idempotence du transfert en cours de saisie. Même patron que
        # `document_form` et `inventory` : posée juste avant l'envoi, CONSERVÉE
        # si l'envoi échoue (le rejeu doit porter la même clé), remise à None
        # dès que le transfert est parti ou mis en file.
        self._cle_idempotence: str | None = None
        # Repli si le serveur ne renvoie pas encore le seuil (version antérieure).
        self._seuil_retard = 3
        # Reprise du panier proposée une seule fois par instance de vue, au
        # premier chargement du stock (voir `_afficher_stock` /
        # `_proposer_reprise_brouillon`).
        self._reprise_proposee = False
        self._build()

    def _cle_brouillon(self) -> str:
        """Clé de fichier du brouillon, propre à CETTE région source.

        Contrairement à `inventaire_regional.py`, `self._panier` est déjà de
        la donnée pure (product_id -> quantité), pas des `StringVar` : aucune
        reprise « en mémoire » entre deux `refresh()` n'était nécessaire
        (le panier vit dans `self`, jamais reconstruit) — mais rien ne le
        protégeait d'une DESTRUCTION de la vue (session expirée, compte
        désactivé, mot de passe changé pendant la saisie ; `main.py` recrée
        toutes les vues à la reconnexion), repéré par audit de robustesse le
        9 septembre 2026.
        """
        return f"regional_transfer_{self.region or 'sans_region'}"

    def _sauver_brouillon(self) -> None:
        if not self._panier:
            effacer_brouillon(self._cle_brouillon())
            return
        lignes = [{"product_id": pid, "quantity": q} for pid, q in self._panier.items()]
        sauvegarder_brouillon(
            self._cle_brouillon(),
            {"lines": lignes, "destination": self.destination_var.get(),
             "note": self.note_var.get()},
            sector=self.sector,
        )

    def _proposer_reprise_brouillon(self) -> None:
        brouillon = charger_brouillon(self._cle_brouillon(), sector=self.sector)
        if not brouillon:
            return
        lignes = brouillon.get("lines") or []
        if not lignes:
            return
        auteur = (brouillon.get("username") or "").strip()
        quand = (brouillon.get("created_at") or "").replace("T", " ")
        detail = t("Commencé le {quand}").format(quand=quand) if quand else ""
        if auteur:
            detail += t(" par {qui}").format(qui=auteur)
        if not messagebox.askyesno(
            t("Transfert interrompu"),
            t("Un transfert n'a pas été envoyé : {n} article(s) déjà "
              "ajouté(s).\n{detail}\n\nReprendre ce transfert ?\n\n"
              "« Non » efface ces saisies et repart d'un panier vide.").format(
                  n=len(lignes), detail=detail),
            parent=self,
        ):
            effacer_brouillon(self._cle_brouillon())
            return
        # Un stock qui a bougé entre-temps (un autre poste a expédié) ne
        # doit pas laisser repartir plus que ce qui est réellement détenu —
        # même garde-fou que `_afficher_stock` applique déjà au panier
        # courant.
        dispo_par_id = {a["product_id"]: (a.get("quantite") or 0) for a in self._stock}
        restaures = 0
        for ligne in lignes:
            pid, qte = ligne.get("product_id"), ligne.get("quantity")
            if pid is None or qte is None or qte > dispo_par_id.get(pid, 0) + 1e-9:
                continue
            self._panier[pid] = qte
            restaures += 1
        if brouillon.get("destination"):
            self.destination_var.set(brouillon["destination"])
        if brouillon.get("note"):
            self.note_var.set(brouillon["note"])
        self._peupler_panier()
        self._status.configure(
            text=t("Transfert repris : {n} article(s) restauré(s).").format(
                n=restaures),
            text_color=GRIS)

    # ------------------------------------------------------------ interface

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 8))
        self._titre = ctk.CTkLabel(
            top, text=self._libelle_titre(),
            font=ctk.CTkFont(size=20, weight="bold"), text_color="#f0f0f0")
        self._titre.pack(side="left")
        ctk.CTkButton(top, text=t("↻  Rafraîchir"), width=110,
                      fg_color=BORDURE, hover_color="#353840",
                      corner_radius=8, command=self.refresh).pack(side="right", padx=8)

        if self.mode_admin:
            aide = t("Transferts partis de cette région et pas encore "
                     "confirmés à l'arrivée. Lecture seule : c'est la région "
                     "qui expédie, pas le central à sa place.")
        else:
            aide = t("Envoie directement à une région voisine, sans repasser "
                     "par l'entrepôt central. La marchandise quitte ton stock "
                     "tout de suite ; elle n'entre dans le sien qu'une fois "
                     "qu'elle aura confirmé sa réception.")
        ctk.CTkLabel(self, text=aide, text_color=GRIS,
                     font=ctk.CTkFont(size=11), wraplength=900,
                     justify="left").pack(anchor="w", padx=20, pady=(0, 6))

        if not self.mode_admin:
            self._build_formulaire()
        self._build_sortants()

        self._status = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11),
                                    text_color=GRIS, wraplength=900,
                                    justify="left")
        self._status.pack(anchor="w", padx=20, pady=(0, 14))

    # ---- formulaire d'envoi (compte régional uniquement) ----

    def _build_formulaire(self):
        cadre = ctk.CTkFrame(self, fg_color=FOND, corner_radius=10,
                             border_width=1, border_color=BORDURE)
        cadre.pack(fill="x", padx=20, pady=(0, 8))

        ligne1 = ctk.CTkFrame(cadre, fg_color="transparent")
        ligne1.pack(fill="x", padx=12, pady=(10, 4))
        ctk.CTkLabel(ligne1, text=t("Région destinataire :"),
                     text_color=GRIS_CLAIR,
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 8))
        self.destination_var = tk.StringVar()
        self._combo_destination = ctk.CTkComboBox(
            ligne1, variable=self.destination_var,
            values=self._destinations(), width=200, state="readonly",
            fg_color="#111318", border_color=BORDURE, button_color=BORDURE,
            button_hover_color="#353840", dropdown_fg_color=FOND)
        self._combo_destination.pack(side="left")

        ctk.CTkLabel(ligne1, text=t("Commentaire :"), text_color=GRIS_CLAIR,
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(16, 8))
        self.note_var = tk.StringVar()
        ctk.CTkEntry(ligne1, textvariable=self.note_var, height=28,
                     placeholder_text=t("Ex. : dépanne Jacmel en filtres"),
                     fg_color="#111318", border_color=BORDURE).pack(
            side="left", fill="x", expand=True)

        ligne2 = ctk.CTkFrame(cadre, fg_color="transparent")
        ligne2.pack(fill="x", padx=12, pady=(4, 10))
        ctk.CTkLabel(ligne2, text=t("Article :"), text_color=GRIS_CLAIR,
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(0, 8))
        self.article_var = tk.StringVar()
        self._combo_article = ctk.CTkComboBox(
            ligne2, variable=self.article_var, values=[], width=340,
            state="readonly", fg_color="#111318", border_color=BORDURE,
            button_color=BORDURE, button_hover_color="#353840",
            dropdown_fg_color=FOND)
        self._combo_article.pack(side="left")

        ctk.CTkLabel(ligne2, text=t("Quantité :"), text_color=GRIS_CLAIR,
                     font=ctk.CTkFont(size=11)).pack(side="left", padx=(16, 8))
        self.quantite_var = tk.StringVar()
        ctk.CTkEntry(ligne2, textvariable=self.quantite_var, width=90,
                     height=28, fg_color="#111318",
                     border_color=BORDURE).pack(side="left")
        ctk.CTkButton(ligne2, text=t("+  Ajouter"), width=110, height=28,
                      corner_radius=6, fg_color=BORDURE,
                      hover_color="#353840",
                      command=self._ajouter_ligne).pack(side="left", padx=10)

        # Le panier : ce qui partira sur le bon. Un envoi porte souvent
        # plusieurs articles (on ne fait pas rouler un camion pour un filtre),
        # d'où l'accumulation avant envoi plutôt qu'un bon par article.
        self._cadre_panier = ctk.CTkScrollableFrame(
            self, fg_color=FOND, corner_radius=10, height=120)
        self._cadre_panier.pack(fill="x", padx=20, pady=(0, 8))

        actions = ctk.CTkFrame(self, fg_color="transparent")
        actions.pack(fill="x", padx=20, pady=(0, 8))
        self._btn_vider = ctk.CTkButton(
            actions, text=t("Vider"), width=100, fg_color=BORDURE,
            hover_color="#353840", command=self._vider_panier)
        self._btn_vider.pack(side="left")
        self._btn_envoyer = ctk.CTkButton(
            actions, text=t("📦  Envoyer le transfert"), width=210,
            fg_color="#166534", hover_color="#15803d", command=self._envoyer)
        self._btn_envoyer.pack(side="right")

    # ---- liste des envois en attente de confirmation ----

    def _build_sortants(self):
        ctk.CTkLabel(self, text=t("Envois en attente de confirmation"),
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=ORANGE).pack(anchor="w", padx=20, pady=(4, 2))

        cadre = ctk.CTkFrame(self, fg_color=FOND, corner_radius=10,
                             border_width=1, border_color=BORDURE)
        cadre.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        colonnes = ("destination", "date", "attente", "articles", "quantite",
                    "par")
        self.tree = ttk.Treeview(cadre, columns=colonnes, show="headings")
        entetes = {"destination": t("Destination"), "date": t("Envoyé le"),
                   "attente": t("En attente"),
                   "articles": t("Contenu"), "quantite": t("Quantité totale"),
                   "par": t("Envoyé par")}
        largeurs = {"destination": 150, "date": 140, "attente": 110,
                    "articles": 340, "quantite": 120, "par": 150}
        for col in colonnes:
            self.tree.heading(col, text=entetes[col])
            self.tree.column(col, width=largeurs[col], anchor="w")
        # Un envoi non confirmé depuis plus de trois jours est de la
        # marchandise sortie d'ici et entrée nulle part : c'est l'expéditeur
        # qui doit relancer, donc c'est ici que ça doit se voir.
        self.tree.tag_configure("retard", background="#5a2323",
                                foreground="#f0c8c8")
        self.tree.pack(side="left", fill="both", expand=True)
        barre = ttk.Scrollbar(cadre, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=barre.set)
        barre.pack(side="right", fill="y")

    def _libelle_titre(self) -> str:
        if self.region:
            return t("🔁  Transfert entre régions — {region}").format(
                region=self.region)
        return t("🔁  Transfert entre régions")

    def definir_region(self, region: str | None):
        """Change la région pilotée (sélecteur de l'écran admin).

        Le panier n'est vidé que si la région PILOTÉE change réellement —
        pas à chaque appel. `_afficher_stock` appelle cette méthode à
        CHAQUE `refresh()` (tab reselectionné, F5, bouton Rafraîchir), avec
        la même région que celle déjà en cours pour un compte régional
        normal (elle ne change jamais pour ce compte) : sans ce garde-fou,
        le panier en cours de saisie était effacé en silence au moindre
        refresh, bien avant même le cas de l'expiration de session — repéré
        par audit de robustesse le 9 septembre 2026.
        """
        nouvelle_region = region or ""
        changement = nouvelle_region != self.region
        self.region = nouvelle_region
        self._titre.configure(text=self._libelle_titre())
        if not self.mode_admin:
            self._combo_destination.configure(values=self._destinations())
            if self.destination_var.get() not in self._destinations():
                self.destination_var.set("")
            if changement:
                self._vider_panier()

    def _destinations(self) -> list[str]:
        """Régions à entrepôt, la sienne exclue : on ne s'envoie pas à soi."""
        return [r for r in self._regions_possibles if r and r != self.region]

    # ------------------------------------------------------------- panier

    def _article_selectionne(self) -> dict | None:
        libelle = self.article_var.get()
        return next((a for a in self._stock if self._libelle_article(a) == libelle),
                    None)

    def _libelle_article(self, article: dict) -> str:
        return t("{nom} · {sku} — {dispo:g} {unite} disponible(s)").format(
            nom=article.get("name", "?"), sku=article.get("sku", "?"),
            dispo=article.get("quantite", 0), unite=article.get("unit", ""))

    def _ajouter_ligne(self):
        article = self._article_selectionne()
        if article is None:
            self._status.configure(text=t("Choisis d'abord un article."),
                                   text_color=ORANGE)
            return
        texte = self.quantite_var.get().strip()
        try:
            quantite = float_saisie(texte)
        except ValueError:
            quantite = -1
        if quantite <= 0:
            self._status.configure(
                text=t("Quantité invalide : saisis un nombre supérieur à zéro."),
                text_color=ORANGE)
            return

        pid = article["product_id"]
        total = self._panier.get(pid, 0.0) + quantite
        dispo = article.get("quantite", 0) or 0
        if total > dispo + 1e-9:
            self._status.configure(
                text=t("« {nom} » : {total:g} demandé(s) alors que ton "
                       "entrepôt n'en détient que {dispo:g}.").format(
                           nom=article.get("name", ""), total=total, dispo=dispo),
                text_color=ORANGE)
            return

        self._panier[pid] = total
        self.quantite_var.set("")
        self._status.configure(text="", text_color=GRIS)
        self._peupler_panier()
        self._sauver_brouillon()

    def _retirer_ligne(self, product_id: int):
        self._panier.pop(product_id, None)
        self._peupler_panier()
        self._sauver_brouillon()

    def _vider_panier(self):
        self._panier.clear()
        self.note_var.set("")
        self._peupler_panier()
        effacer_brouillon(self._cle_brouillon())

    def _peupler_panier(self):
        if not self._cadre_panier.winfo_exists():
            return
        for enfant in self._cadre_panier.winfo_children():
            enfant.destroy()
        if not self._panier:
            ctk.CTkLabel(self._cadre_panier,
                         text=t("Aucun article ajouté pour l'instant."),
                         text_color=GRIS,
                         font=ctk.CTkFont(size=11)).pack(anchor="w", padx=8, pady=6)
            return
        par_id = {a["product_id"]: a for a in self._stock}
        for pid, quantite in self._panier.items():
            article = par_id.get(pid, {})
            ligne = ctk.CTkFrame(self._cadre_panier, fg_color="transparent")
            ligne.pack(fill="x", padx=6, pady=2)
            ctk.CTkLabel(
                ligne,
                text=t("{nom} · {sku} — {q:g} {unite}").format(
                    nom=article.get("name", pid), sku=article.get("sku", ""),
                    q=quantite, unite=article.get("unit", "")),
                text_color="#d1d5db", anchor="w").pack(side="left")
            ctk.CTkButton(ligne, text=t("Retirer"), width=80, height=24,
                          corner_radius=6, fg_color=BORDURE,
                          hover_color="#353840",
                          command=lambda p=pid: self._retirer_ligne(p)).pack(
                side="right")

    # -------------------------------------------------------------- envoi

    def _envoyer(self):
        destination = self.destination_var.get().strip()
        if not destination:
            self._status.configure(text=t("Choisis la région destinataire."),
                                   text_color=ORANGE)
            return
        if not self._panier:
            self._status.configure(
                text=t("Ajoute au moins un article avant d'envoyer."),
                text_color=ORANGE)
            return

        lignes = [{"product_id": pid, "quantity": q}
                  for pid, q in self._panier.items()]
        if not messagebox.askyesno(
                t("Transfert entre régions"),
                t("Envoyer {n} article(s) vers {region} ?\n\nLa marchandise "
                  "sort de ton stock immédiatement et n'entrera dans le sien "
                  "qu'après sa confirmation de réception.").format(
                      n=len(lignes), region=destination),
                parent=self):
            return

        if self._cle_idempotence is None:
            self._cle_idempotence = str(uuid.uuid4())

        self._btn_envoyer.configure(state="disabled", text=t("Envoi…"))
        note = self.note_var.get().strip()

        # Horodatage posé AVANT l'envoi, comme dans `inventaire_regional` :
        # c'est le moment où le camion part. Si l'envoi tombe en file
        # d'attente et n'est rejoué qu'à la reprise du réseau, le bon doit
        # rester daté du départ réel, pas du jour de la reconnexion.
        payload_envoi = {
            "region_destination": destination, "lines": lignes,
            "region_source": self.region or None, "note": note,
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            # Part AVEC le bon, y compris en file d'attente : c'est elle qui
            # empêche un rejeu de créer un second transfert et de débiter deux
            # fois l'entrepôt d'origine.
            "idempotency_key": self._cle_idempotence,
        }

        def _restaurer():
            if self._btn_envoyer.winfo_exists():
                self._btn_envoyer.configure(
                    state="normal", text=t("📦  Envoyer le transfert"))

        def _ok(_res):
            _restaurer()
            # Transfert enregistré : le prochain envoi est un autre transfert,
            # il lui faut sa propre clé.
            self._cle_idempotence = None
            messagebox.showinfo(
                t("Transfert envoyé"),
                t("Transfert enregistré. {region} le verra dans son écran "
                  "« Réception régionale » et devra confirmer ce qu'elle "
                  "reçoit.").format(region=destination))
            self._vider_panier()
            self.refresh()

        def _err(exc):
            _restaurer()
            erreur = str(exc)
            # Même détection que `document_form._on_submit_error` et
            # `inventaire_regional` : la locution « contacter le serveur »
            # vient de `api_client._message_serveur_injoignable`, et
            # « timeout » couvre les délais dépassés. Tout le reste est un
            # refus MÉTIER du serveur (stock insuffisant, région inconnue) et
            # ne doit surtout pas partir en file d'attente : le rejeu
            # échouerait à l'identique.
            if "contacter le serveur" in erreur.lower() or "timeout" in erreur.lower():
                if messagebox.askyesno(
                        t("Serveur injoignable"),
                        t("Le serveur ne répond pas.\n\nMettre ce transfert "
                          "en file d'attente ?\nIl sera envoyé automatiquement "
                          "au retour du réseau, daté d'aujourd'hui — pas du "
                          "jour de la reprise.\n\n⚠ Attention : ton stock "
                          "n'a pas été vérifié par le serveur."),
                        parent=self):
                    n = mettre_en_attente("regional_transfer", payload_envoi,
                                          username=self.api.username)
                    messagebox.showinfo(
                        t("En file d'attente"),
                        t("Transfert mis en attente ({n} en file).\nIl sera "
                          "envoyé dès que le serveur sera joignable.").format(n=n),
                        parent=self)
                    # La clé est partie avec le payload mis en file : elle a
                    # fait son travail, le prochain envoi aura la sienne.
                    self._cle_idempotence = None
                    # Le transfert est conservé dans la file : garder le
                    # panier ferait ressaisir — et donc réexpédier — le même
                    # envoi une seconde fois.
                    self._vider_panier()
                    self.destination_var.set("")
                    self._status.configure(text="", text_color=GRIS)
                    return
            self._status.configure(text=erreur, text_color=ORANGE)

        run_async(self,
                  lambda: self.api.creer_transfert_regional(**payload_envoi),
                  _ok, _err)

    # ------------------------------------------------------------- données

    def refresh(self):
        self._status.configure(text=t("Chargement…"), text_color=GRIS)
        if not self.mode_admin:
            run_async(self,
                      lambda: self.api.get_stock_regional(
                          self.region or None, inclure_catalogue=True),
                      self._afficher_stock,
                      lambda exc: self._status.configure(text=str(exc),
                                                         text_color=ORANGE))
        run_async(self,
                  lambda: self.api.get_transferts_sortants(self.region or None),
                  self._afficher_sortants,
                  lambda exc: self._status.configure(text=str(exc),
                                                     text_color=ORANGE))

    def _afficher_stock(self, data):
        self.definir_region(data.get("region") or self.region)
        # TOUT le catalogue actif, y compris ce que la région ne détient pas :
        # le libellé porte alors « 0 <unité> disponible(s) », et `_ajouter_ligne`
        # refuse de le mettre au panier. Voir l'en-tête du module.
        self._stock = list(data.get("articles") or [])
        self._combo_article.configure(
            values=[self._libelle_article(a) for a in self._stock])
        if not self._stock:
            self.article_var.set("")
        # Le panier peut référencer un article qui n'est plus disponible en
        # quantité suffisante (un autre poste a expédié entre-temps) : le
        # purger vaut mieux que de laisser partir un envoi que le serveur
        # refusera ligne par ligne.
        dispo_par_id = {a["product_id"]: (a.get("quantite") or 0)
                        for a in self._stock}
        for pid, quantite in list(self._panier.items()):
            if quantite > dispo_par_id.get(pid, 0) + 1e-9:
                self._panier.pop(pid)
        self._peupler_panier()
        # Proposé une seule fois par instance, et seulement si rien n'est
        # déjà en cours de saisie ici (un panier non vide veut dire que
        # cette instance a déjà repris — ou constitué — son propre panier ;
        # reproposer romprait ce travail en cours).
        if not self._reprise_proposee:
            self._reprise_proposee = True
            if not self._panier:
                self._proposer_reprise_brouillon()

    def _afficher_sortants(self, data):
        if not self.mode_admin:
            self.definir_region(data.get("region") or self.region)
        self._sortants = data.get("transferts") or []
        self._seuil_retard = data.get("seuil_relance_jours") or 3
        self.tree.delete(*self.tree.get_children())
        en_retard = 0
        for doc in self._sortants:
            lignes = doc.get("lines") or []
            total = sum(l.get("quantity", 0) for l in lignes)
            retard = bool(doc.get("en_retard"))
            en_retard += 1 if retard else 0
            self.tree.insert("", "end", iid=str(doc["id"]),
                             tags=("retard",) if retard else (), values=(
                doc.get("region") or "—",
                (doc.get("created_at") or "")[:16],
                self._libelle_attente(doc.get("jours_en_attente")),
                self._resume_articles(lignes),
                f"{total:g}",
                doc.get("created_by") or "—",
            ))
        if not self._sortants:
            self._status.configure(text=t("Aucun envoi en attente."),
                                   text_color=GRIS)
        elif en_retard:
            self._status.configure(
                text=t("{n} envoi(s) en attente, dont {r} depuis plus de {j} "
                       "jour(s) (lignes en rouge) : appelle la région "
                       "destinataire pour qu'elle confirme.").format(
                           n=len(self._sortants), r=en_retard,
                           j=self._seuil_retard),
                text_color="#ef4444")
        else:
            self._status.configure(
                text=t("{n} envoi(s) en attente de confirmation par la région "
                       "destinataire.").format(n=len(self._sortants)),
                text_color=GRIS)

    def _libelle_attente(self, jours) -> str:
        """« 2,4 j » — l'ancienneté vient du serveur, jamais de l'heure du poste."""
        if jours is None:
            return "—"
        return t("{jours} j").format(jours=f"{jours:g}".replace(".", ","))

    def _resume_articles(self, lignes: list[dict]) -> str:
        """Même présentation que l'écran de réception : « Nom · CODE »."""
        if not lignes:
            return "—"
        noms = [f"{l.get('name', '?')} · {l.get('sku', '?')}" for l in lignes[:3]]
        texte = ", ".join(noms)
        if len(lignes) > 3:
            texte += ", " + t("+{n} autre(s)").format(n=len(lignes) - 3)
        return texte
