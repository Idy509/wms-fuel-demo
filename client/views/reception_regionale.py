"""Réception régionale : confirmer ce que la région a réellement reçu.

Écran d'un compte régional. Les transferts partis de l'entrepôt central
attendent ici : le magasinier de la région saisit ce qu'il a effectivement
compté à l'arrivée, produit par produit. Un écart n'est jamais bloquant — ce
qui compte pour le stock de la région est ce qui est confirmé reçu.

Si le serveur est injoignable au moment de confirmer, la confirmation part en
file d'attente hors ligne (`file_attente`, type « confirmation_reception »)
plutôt que d'être perdue : une région en coupure Internet peut compter sa
livraison et décharger le camion sans attendre le retour du réseau.

Un admin ne confirme plus à la place de la région (le serveur le refuse
désormais, 403) : la trace `received_by` doit porter le nom de qui a
réellement compté, pas celui du central qui relaierait un appel téléphonique.
Un admin qui ouvre cet écran voit donc la même liste de transferts en
transit, mais sans bouton de confirmation.
"""
import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk

from async_call import run_async
from file_attente import ajouter as mettre_en_attente
from i18n import t
from views.utils import float_saisie


class ReceptionRegionaleView(ctk.CTkFrame):

    def __init__(self, master, api, region: str | None = None,
                 user_role: str | None = None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self.region = region or ""
        self.user_role = user_role or ""
        self.mode_admin = self.user_role == "admin"
        self._transferts: list[dict] = []
        # Repli tant que le serveur n'a pas répondu (ou s'il est d'une version
        # antérieure qui ne renvoie pas encore le seuil) : la même valeur que
        # SEUIL_TRANSFERT_EN_ATTENTE_JOURS côté serveur.
        self._seuil_retard = 3
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
            texte_aide = t("Bons en route vers la région — venus de l'entrepôt "
                           "central ou d'une région voisine — en attente "
                           "qu'elle confirme ce qu'elle a reçu. Lecture "
                           "seule : c'est à la région de confirmer, pas au "
                           "central à sa place.")
        else:
            texte_aide = t("Les bons ci-dessous sont en route vers ton "
                           "entrepôt : la colonne « Provenance » dit d'où. "
                           "Sélectionne un bon, puis clique sur « Confirmer "
                           "la réception » pour saisir les quantités "
                           "réellement reçues.")
        ctk.CTkLabel(
            self, text=texte_aide, text_color="#6b7280", font=ctk.CTkFont(size=11),
            wraplength=900, justify="left").pack(anchor="w", padx=20, pady=(0, 6))

        table_frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                                   border_width=1, border_color="#2a2d35")
        table_frame.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        colonnes = ("provenance", "reference", "date", "attente", "articles",
                    "quantite", "expedie_par", "note")
        self.tree = ttk.Treeview(table_frame, columns=colonnes, show="headings")
        entetes = {"provenance": t("Provenance"), "reference": t("Référence"),
                   "date": t("Expédié le"), "attente": t("En attente"),
                   "articles": t("Contenu"), "quantite": t("Quantité totale"),
                   "expedie_par": t("Expédié par"), "note": t("Commentaire")}
        # « Contenu » (les noms d'articles) prend la place laissée par
        # « Commentaire », presque toujours vide : sans les noms, la région
        # ne sait pas ce qu'elle va recevoir avant d'ouvrir la confirmation.
        #
        # « Provenance » en tête : depuis le transfert région -> région, un
        # colis peut venir du central OU d'une région voisine, et la marche à
        # suivre en cas d'écart n'est pas la même (on rappelle l'expéditeur).
        largeurs = {"provenance": 140, "reference": 120, "date": 140,
                    "attente": 110, "articles": 280, "quantite": 110,
                    "expedie_par": 150, "note": 130}
        for col in colonnes:
            self.tree.heading(col, text=entetes[col])
            self.tree.column(col, width=largeurs[col], anchor="w")
        # Un transfert qui traîne depuis plus de trois jours n'est plus « en
        # route » : c'est de la marchandise sortie d'un entrepôt et entrée dans
        # aucun autre. La ligne passe en rouge pour qu'on décroche le téléphone
        # au lieu d'attendre encore. Même code couleur que les alertes
        # régionales (orange = attention, rouge = grave).
        self.tree.tag_configure("retard", background="#5a2323",
                                foreground="#f0c8c8")
        self.tree.pack(side="left", fill="both", expand=True)
        barre = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=barre.set)
        barre.pack(side="right", fill="y")
        if not self.mode_admin:
            self.tree.bind("<Double-1>", lambda _e: self._confirmer())

        bas = ctk.CTkFrame(self, fg_color="transparent")
        bas.pack(fill="x", padx=20, pady=(0, 16))
        self._status = ctk.CTkLabel(bas, text="", font=ctk.CTkFont(size=11),
                                    text_color="#6b7280")
        self._status.pack(side="left")
        if not self.mode_admin:
            ctk.CTkButton(bas, text=t("✓  Confirmer la réception"), width=200,
                          fg_color="#166534", hover_color="#15803d",
                          command=self._confirmer).pack(side="right")

    def _libelle_titre(self) -> str:
        if self.mode_admin:
            if self.region:
                return t("📥  Transferts en attente — {region}").format(
                    region=self.region)
            return t("📥  Transferts en attente de réception")
        if self.region:
            return t("📥  Réception — {region}").format(region=self.region)
        return t("📥  Réception régionale")

    def definir_region(self, region: str | None):
        self.region = region or ""
        self._titre.configure(text=self._libelle_titre())

    # ------------------------------------------------------------- données

    def refresh(self):
        self._status.configure(text=t("Chargement…"), text_color="#6b7280")
        run_async(self,
                  lambda: self.api.get_transferts_en_transit(self.region or None),
                  self._afficher, self._erreur)

    def _erreur(self, exc):
        self._status.configure(text=str(exc), text_color="orange")

    def _afficher(self, data):
        self.definir_region(data.get("region"))
        self._transferts = data.get("transferts", [])
        self._seuil_retard = data.get("seuil_relance_jours") or 3
        self.tree.delete(*self.tree.get_children())
        en_retard = 0
        for doc in self._transferts:
            lignes = doc.get("lines", [])
            total = sum(l.get("quantity", 0) for l in lignes)
            retard = bool(doc.get("en_retard"))
            en_retard += 1 if retard else 0
            self.tree.insert("", "end", iid=str(doc["id"]),
                             tags=("retard",) if retard else (), values=(
                doc.get("provenance") or doc.get("source_region")
                or t("Entrepôt central"),
                doc.get("reference") or f"#{doc['id']}",
                (doc.get("created_at") or "")[:16],
                self._libelle_attente(doc.get("jours_en_attente")),
                self._resume_articles(lignes),
                f"{total:g}",
                doc.get("created_by") or doc.get("operator") or "—",
                (doc.get("note") or "").replace("\n", " "),
            ))
        if not self._transferts:
            self._status.configure(text=t("Aucun transfert en attente"),
                                   text_color="#6b7280")
        elif en_retard:
            self._status.configure(
                text=t("{n} transfert(s) en attente, dont {r} depuis plus de "
                       "{j} jour(s) (lignes en rouge) : rappelle l'expéditeur "
                       "ou confirme ce que tu as reçu.").format(
                           n=len(self._transferts), r=en_retard,
                           j=self._seuil_retard),
                text_color="#ef4444")
        else:
            self._status.configure(
                text=t("{n} transfert(s) en attente de confirmation").format(
                    n=len(self._transferts)),
                text_color="#6b7280")

    def _libelle_attente(self, jours) -> str:
        """« 2,4 j » — l'ancienneté vient du serveur, jamais de l'heure du poste.

        Un poste d'entrepôt mal réglé afficherait sinon des attentes fausses,
        et c'est justement sur cette colonne qu'on décide de relancer.
        """
        if jours is None:
            return "—"
        return t("{jours} j").format(jours=f"{jours:g}".replace(".", ","))

    def _resume_articles(self, lignes: list[dict]) -> str:
        """« Oil Engine (15W40) · OIL-01, Acid · EL - 137 ».

        Un point médian, pas des parenthèses : certains noms de produits en
        contiennent déjà (ex. « Oil Engine (15W40) »), et imbriquer des
        parenthèses donnait un affichage illisible. Pas de quantité par
        article ici : la colonne « Quantité totale » existe déjà à côté.
        """
        if not lignes:
            return "—"
        noms = [f"{l.get('name', '?')} · {l.get('sku', '?')}" for l in lignes[:3]]
        texte = ", ".join(noms)
        if len(lignes) > 3:
            texte += ", " + t("+{n} autre(s)").format(n=len(lignes) - 3)
        return texte

    def _selection(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            self._status.configure(text=t("Sélectionne d'abord un transfert"),
                                   text_color="orange")
            return None
        doc_id = int(sel[0])
        return next((d for d in self._transferts if d["id"] == doc_id), None)

    def _confirmer(self):
        doc = self._selection()
        if doc:
            ConfirmationReceptionDialog(self, self.api, doc, on_saved=self.refresh)


class ConfirmationReceptionDialog(ctk.CTkToplevel):
    """Saisie des quantités reçues, ligne par ligne.

    Chaque champ est pré-rempli avec la quantité expédiée : le cas courant
    (tout est arrivé) ne demande qu'un clic. L'écart est calculé et montré,
    mais n'empêche jamais de valider.
    """

    def __init__(self, master, api, document: dict, on_saved=None):
        super().__init__(master)
        self.api = api
        self.document = document
        self.on_saved = on_saved
        self.title(t("Réception — {ref}").format(
            ref=document.get("reference") or "#" + str(document["id"])))
        self.geometry("640x520")
        self.transient(master)
        self.lift()
        self.grab_set()

        self._champs: dict[int, tk.StringVar] = {}
        self._ecart_labels: dict[int, ctk.CTkLabel] = {}
        self._build()

    def _build(self):
        ctk.CTkLabel(self, text=t("Quantités réellement reçues"),
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(14, 2))
        ctk.CTkLabel(
            self,
            text=t("Corrige la quantité si tout n'est pas arrivé. Un écart "
                   "est simplement signalé : il ne bloque rien."),
            text_color="#6b7280", font=ctk.CTkFont(size=11),
            wraplength=560).pack(pady=(0, 8))

        cadre = ctk.CTkScrollableFrame(self, fg_color="#1a1d23", corner_radius=10)
        cadre.pack(fill="both", expand=True, padx=16, pady=(0, 8))

        entetes = ("Article", "Expédié", "Reçu", "Écart")
        for col, texte in enumerate(entetes):
            ctk.CTkLabel(cadre, text=t(texte), font=ctk.CTkFont(size=11, weight="bold"),
                         text_color="#9ca3af").grid(row=0, column=col, padx=8,
                                                    pady=(6, 4), sticky="w")

        for i, ligne in enumerate(self.document.get("lines", []), start=1):
            pid = ligne["product_id"]
            # « Nom · CODE » et pas « Nom (CODE) » : la plupart des noms du
            # catalogue contiennent déjà des parenthèses (« Fuel Filter (HPU
            # (3/4/5)) »), et en imbriquer une de plus rendait la ligne
            # illisible. Même règle que dans la liste des transferts.
            ctk.CTkLabel(cadre, text=f"{ligne.get('name', '')} · {ligne.get('sku', '')}",
                         text_color="#d1d5db", anchor="w").grid(
                row=i, column=0, padx=8, pady=3, sticky="w")
            ctk.CTkLabel(cadre, text=f"{ligne['quantity']:g}",
                         text_color="#9ca3af").grid(row=i, column=1, padx=8, pady=3)

            var = tk.StringVar(value=f"{ligne['quantity']:g}")
            var.trace_add("write", lambda *_, p=pid: self._maj_ecart(p))
            self._champs[pid] = var
            ctk.CTkEntry(cadre, textvariable=var, width=90).grid(
                row=i, column=2, padx=8, pady=3)

            lbl = ctk.CTkLabel(cadre, text="—", text_color="#6b7280", width=90)
            lbl.grid(row=i, column=3, padx=8, pady=3)
            self._ecart_labels[pid] = lbl

        ctk.CTkLabel(self, text=t("Commentaire (facultatif)"),
                     text_color="#9ca3af", font=ctk.CTkFont(size=11)).pack(
            anchor="w", padx=18)
        self.note_var = tk.StringVar()
        ctk.CTkEntry(self, textvariable=self.note_var,
                     placeholder_text=t("Ex. : un carton ouvert à l'arrivée")).pack(
            fill="x", padx=16, pady=(2, 6))

        self._status = ctk.CTkLabel(self, text="", text_color="orange",
                                    font=ctk.CTkFont(size=11), wraplength=580)
        self._status.pack(padx=16)

        boutons = ctk.CTkFrame(self, fg_color="transparent")
        boutons.pack(fill="x", padx=16, pady=(4, 12))
        ctk.CTkButton(boutons, text=t("Annuler"), width=110, fg_color="gray30",
                      command=self.destroy).pack(side="right", padx=6)
        self._bouton = ctk.CTkButton(boutons, text=t("Confirmer la réception"),
                                     width=190,
                                     fg_color="#166534", hover_color="#15803d",
                                     command=self._valider)
        self._bouton.pack(side="right")

    def _quantite(self, pid: int) -> float | None:
        texte = self._champs[pid].get().strip()
        if not texte:
            return None
        try:
            valeur = float_saisie(texte)
        except ValueError:
            return None
        return valeur if valeur >= 0 else None

    def _maj_ecart(self, pid: int):
        ligne = next(l for l in self.document["lines"] if l["product_id"] == pid)
        recu = self._quantite(pid)
        label = self._ecart_labels[pid]
        if recu is None:
            label.configure(text="?", text_color="#f59e0b")
            return
        ecart = round(recu - ligne["quantity"], 3)
        if abs(ecart) < 1e-9:
            label.configure(text=t("conforme"), text_color="#22c55e")
        elif ecart > 0:
            # Recevoir plus que l'expédié est refusé : le signaler tout de
            # suite évite de remplir tout le formulaire pour rien.
            label.configure(text=t("{ecart:+g} — impossible").format(ecart=ecart),
                            text_color="#ef4444")
        else:
            label.configure(text=f"{ecart:+g}", text_color="#f59e0b")

    def _valider(self):
        lignes = []
        for ligne in self.document.get("lines", []):
            pid = ligne["product_id"]
            recu = self._quantite(pid)
            if recu is None:
                self._status.configure(
                    text=t("Quantité invalide pour « {nom} » (nombre positif "
                           "ou zéro attendu)").format(nom=ligne.get("name", pid)))
                return
            # On ne peut pas recevoir plus que ce qui est parti : le serveur le
            # refuse, autant le dire ici avant l'aller-retour réseau.
            if recu > ligne["quantity"] + 1e-9:
                self._status.configure(
                    text=t("« {nom} » : {recu:g} reçus alors que {envoye:g} "
                           "seulement ont été expédiés").format(
                               nom=ligne.get("name", pid), recu=recu,
                               envoye=ligne["quantity"]))
                return
            lignes.append({"product_id": pid, "received_quantity": recu})

        ecarts = [l for l, ref in zip(lignes, self.document["lines"])
                  if abs(l["received_quantity"] - ref["quantity"]) > 1e-9]
        if ecarts and not messagebox.askyesno(
                t("Écart constaté"),
                t("{n} article(s) reçus en quantité différente de celle "
                  "expédiée.\n\nSeules les quantités reçues seront ajoutées "
                  "au stock de la région.\n\nConfirmer quand même ?").format(
                      n=len(ecarts)),
                parent=self):
            return

        self._bouton.configure(state="disabled", text=t("Enregistrement…"))

        # Le bon visé est identifié par son id : contrairement à la création
        # d'un bon, une confirmation rejouée agit sur quelque chose qui
        # existait déjà. Si ce bon est annulé entre la mise en attente et le
        # rejeu, `file_attente.rejouer` reçoit un refus métier du serveur, le
        # retire de la file et le rapporte — il ne boucle pas dessus.
        payload_envoi = {
            "document_id": self.document["id"], "lines": lignes,
            "note": self.note_var.get().strip(),
        }

        def _ok(resultat):
            nb = len(resultat.get("ecarts_reception") or [])
            if nb:
                messagebox.showwarning(
                    t("Réception confirmée"),
                    t("Réception enregistrée avec {n} écart(s).\nL'entrepôt "
                      "central en est informé.").format(n=nb))
            else:
                messagebox.showinfo(
                    t("Réception confirmée"),
                    t("Réception enregistrée, tout est conforme."))
            if self.on_saved:
                self.on_saved()
            self.destroy()

        def _err(exc):
            if not self.winfo_exists():
                return
            self._bouton.configure(state="normal",
                                   text=t("Confirmer la réception"))
            erreur = str(exc)
            # Même détection que `document_form._on_submit_error` et
            # `inventaire_regional` : la locution « contacter le serveur »
            # vient de `api_client._message_serveur_injoignable`, et
            # « timeout » couvre les délais dépassés. Un refus métier (bon
            # déjà confirmé, quantité supérieure à l'expédié, compte sans
            # droit) ne part jamais en file : le rejeu échouerait pareil.
            if "contacter le serveur" in erreur.lower() or "timeout" in erreur.lower():
                if messagebox.askyesno(
                        t("Serveur injoignable"),
                        t("Le serveur ne répond pas.\n\nMettre cette "
                          "confirmation de réception en file d'attente ?\nElle "
                          "sera envoyée automatiquement au retour du réseau, "
                          "avec les quantités que tu viens de compter."),
                        parent=self):
                    n = mettre_en_attente("confirmation_reception", payload_envoi,
                                          username=self.api.username)
                    messagebox.showinfo(
                        t("En file d'attente"),
                        t("Confirmation mise en attente ({n} en file).\nElle "
                          "sera envoyée dès que le serveur sera joignable. Le "
                          "stock de ta région ne sera crédité qu'à ce "
                          "moment-là.").format(n=n),
                        parent=self)
                    # La confirmation est conservée dans la file : rouvrir ce
                    # bon pour la ressaisir enverrait deux fois la même
                    # réception. La liste est rafraîchie comme après un
                    # enregistrement réussi.
                    if self.on_saved:
                        self.on_saved()
                    self.destroy()
                    return
            self._status.configure(text=erreur)

        run_async(self,
                  lambda: self.api.confirmer_reception(**payload_envoi),
                  _ok, _err)
