import tkinter as tk
import uuid
from datetime import datetime
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from api_client import ApiError
from async_call import run_async
from i18n import t
from recherche import filtrer
from views.utils import (
    ecarts_a_re_saisir, float_saisie, saisies_concordent,
    seuil_ecart_important, sort_treeview as _sort_treeview,
)
from file_attente import ajouter as mettre_en_attente
from local_cache import (
    charger_comptage, charger_produits, effacer_comptage,
    sauvegarder_comptage, sauvegarder_produits,
)
from operator_context import remplacer_operateur_defaut
from reference_data import warehouse_operators

# Clé du brouillon de comptage. L'inventaire physique de cet écran porte sur
# l'entrepôt central (region = ""), d'où une clé unique ; le paramètre existe
# pour que l'écran régional, s'il reprend un jour la même mécanique, range son
# brouillon dans un fichier distinct plutôt que d'écraser celui-ci.
CLE_COMPTAGE = "general"


class InventoryView(ctk.CTkFrame):
    """Inventaire physique complet : l'opérateur compte TOUS les produits.

    Pour Fuel (secteur sans catalogue multi-produits — juste une ou deux
    cuves), le même mécanisme sert de « jaugeage » : la lecture du niveau de
    cuve, faite deux fois par jour (matin avant livraisons, soir avant
    fermeture), qui corrige le stock théorique comme n'importe quel écart
    d'inventaire.
    """

    def __init__(self, master, api, default_operator: str = "", on_queue_change=None,
                 sector: str | None = None, role: str | None = None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self.sector = sector
        self._is_fuel = sector == "FUEL"
        self._role = role or "magasinier"
        self.products: list[dict] = []
        self._comptages: dict[str, dict] = {}
        self._cle_idempotence: str | None = None
        self._on_queue_change = on_queue_change
        self._en_cours = False
        # La reprise d'un comptage interrompu n'est proposée qu'une fois par
        # session d'application : l'écran est rouvert à chaque changement
        # d'onglet, et reposer la question à chaque fois serait insupportable.
        self._reprise_proposee = False
        self._build(default_operator)

    def _build(self, default_operator: str):
        if self._is_fuel:
            titre = t("⛽  Jaugeage de cuve")
            sous_titre = t("Relève le niveau de la cuve — matin avant les "
                           "livraisons, soir avant la fermeture.")
        else:
            titre = t("📋  Inventaire physique")
            sous_titre = t("Lance un inventaire pour compter tous les produits de "
                           "l'entrepôt.")
        ctk.CTkLabel(self, text=titre,
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(anchor="w", padx=20, pady=(20, 2))
        self._sous_titre = ctk.CTkLabel(self, text=sous_titre, text_color="#9ca3af")
        self._sous_titre.pack(anchor="w", padx=20, pady=(0, 8))

        entete = ctk.CTkFrame(self, fg_color="transparent")
        entete.pack(fill="x", padx=20, pady=4)

        self.operateur_var = tk.StringVar(value=default_operator)
        self.date_var = tk.StringVar(value=datetime.now().strftime("%Y-%m-%d"))

        ctk.CTkLabel(entete, text=t("Opérateur :")).grid(row=0, column=0, sticky="w", padx=4, pady=4)
        ctk.CTkComboBox(entete, variable=self.operateur_var, values=warehouse_operators(),
                        width=180, state="readonly").grid(row=0, column=1, padx=4, pady=4)
        ctk.CTkLabel(entete, text=t("Date :")).grid(row=0, column=2, sticky="w", padx=4, pady=4)
        ctk.CTkEntry(entete, textvariable=self.date_var, width=150,
                     state="readonly").grid(row=0, column=3, padx=4, pady=4)

        barre = ctk.CTkFrame(self, fg_color="transparent")
        barre.pack(fill="x", padx=20, pady=(8, 4))

        self._libelle_demarrer = (t("▶  Démarrer le jaugeage") if self._is_fuel
                                  else t("▶  Démarrer l'inventaire"))
        self._libelle_terminer = (t("✓  Terminer le jaugeage") if self._is_fuel
                                  else t("✓  Terminer l'inventaire"))
        self.btn_demarrer = ctk.CTkButton(
            barre, text=self._libelle_demarrer, width=200,
            fg_color="#2563eb", hover_color="#1d4ed8",
            corner_radius=8, command=self._demarrer)
        self.btn_demarrer.pack(side="left")

        self.btn_terminer = ctk.CTkButton(
            barre, text=self._libelle_terminer, width=200,
            fg_color="#22c55e", hover_color="#16a34a",
            corner_radius=8, command=self._terminer, state="disabled")
        self.btn_terminer.pack(side="left", padx=12)

        self.btn_annuler = ctk.CTkButton(
            barre, text=t("✕  Annuler"), width=100, fg_color="#2a2d35",
            hover_color="#353840", corner_radius=8,
            command=self._annuler_inventaire, state="disabled")
        self.btn_annuler.pack(side="left")

        self._compteur_label = ctk.CTkLabel(barre, text="", text_color="#9ca3af")
        self._compteur_label.pack(side="right", padx=8)

        # --------------------------------------------------------- douchette
        # Une douchette USB se comporte comme un clavier : elle tape le code
        # puis Entrée. Ce champ suffit donc à la faire fonctionner, sans aucun
        # pilote ni réglage — et il se tape aussi bien à la main.
        scan_frame = ctk.CTkFrame(self, fg_color="transparent")
        scan_frame.pack(fill="x", padx=20, pady=(6, 0))
        self.scan_var = tk.StringVar()
        self.scan_entry = ctk.CTkEntry(
            scan_frame, textvariable=self.scan_var,
            placeholder_text=t("🔦  Scanne un code-barres, puis Entrée"),
            width=320, corner_radius=8, height=34,
            fg_color="#141821", border_color="#2563eb")
        self.scan_entry.pack(side="left")
        self.scan_entry.bind("<Return>", self._scanner)
        # <KP_Enter> : certaines douchettes envoient l'Entrée du pavé
        # numérique, que Tk distingue de la touche Entrée principale.
        self.scan_entry.bind("<KP_Enter>", self._scanner)
        self._scan_label = ctk.CTkLabel(scan_frame, text="", text_color="#9ca3af")
        self._scan_label.pack(side="left", padx=12)

        search_frame = ctk.CTkFrame(self, fg_color="transparent")
        search_frame.pack(fill="x", padx=20, pady=(4, 0))
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._filtrer())
        ctk.CTkEntry(search_frame, textvariable=self.search_var,
                     placeholder_text=t("🔍  Filtrer les produits..."),
                     width=280, corner_radius=8, height=32,
                     fg_color="#1a1d23", border_color="#2a2d35").pack(side="left")

        cadre = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                             border_width=1, border_color="#2a2d35")
        cadre.pack(fill="both", expand=True, padx=20, pady=8)

        colonnes = ("sku", "nom", "compte")
        self.tree = ttk.Treeview(cadre, columns=colonnes, show="headings", height=16)
        for col, texte, largeur in (("sku", "SKU", 150), ("nom", t("Produit"), 400),
                                     ("compte", t("Compté"), 120)):
            self.tree.heading(col, text=texte,
                              command=lambda c=col: _sort_treeview(self.tree, c))
            self.tree.column(col, width=largeur, anchor="w")
        self.tree.tag_configure("compte", foreground="#5fd08a")
        self.tree.tag_configure("non_compte", foreground="#888888")
        self.tree.pack(side="left", fill="both", expand=True)

        scrollbar = ttk.Scrollbar(cadre, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<Double-1>", self._saisir_comptage)

        bas = ctk.CTkFrame(self, fg_color="transparent")
        bas.pack(fill="x", padx=16, pady=(0, 16))

        self.status_label = ctk.CTkLabel(bas, text="", text_color="orange")
        self.status_label.pack(side="left")

        # Le curseur est dans le champ de scan dès l'ouverture : l'opérateur
        # qui arrive avec sa douchette n'a rien à cliquer. `after` et non un
        # appel direct : le widget n'est pas encore affiché à cet instant.
        self.after(200, self.focus_scan)

    # ------------------------------------------------------------- douchette

    def focus_scan(self):
        """Remet le curseur dans le champ de scan, sans jamais lever.

        Appelée après CHAQUE scan (trouvé ou non) : sans cela, il faudrait
        recliquer dans le champ entre deux articles. L'écran peut avoir été
        fermé entre-temps (changement d'onglet), d'où les gardes.
        """
        try:
            if self.winfo_exists() and self.scan_entry.winfo_exists():
                self.scan_entry.focus_set()
        except Exception:
            pass

    def _ligne_du_produit(self, product_id):
        """item_id de la ligne de comptage de ce produit, ou None."""
        for item_id, info in self._comptages.items():
            if info.get("product_id") == product_id:
                return item_id
        return None

    def _scanner(self, event=None):
        """Traite un code scanné (ou tapé) et validé par Entrée."""
        code = self.scan_var.get().strip()
        # Vidé AVANT l'appel réseau : la douchette suivante ne doit jamais
        # venir s'ajouter au bout du code précédent.
        self.scan_var.set("")
        self.focus_scan()
        if not code:
            return "break"
        if not self._en_cours:
            self._scan_label.configure(
                text=t("Démarre l'inventaire avant de scanner."),
                text_color="orange")
            return "break"

        self._scan_label.configure(text=t("Recherche du code {code}…").format(
            code=code), text_color="#9ca3af")

        def _ok(produit):
            if self.winfo_exists():
                self._scan_resultat(code, produit)

        def _err(exc):
            if self.winfo_exists():
                # Serveur injoignable ou session expirée : le dire sans rien
                # bloquer, la saisie manuelle reste possible.
                self._scan_label.configure(text=str(exc), text_color="orange")
                self.focus_scan()

        run_async(self, lambda: self.api.scan_barcode(code), _ok, _err)
        return "break"

    def _scan_resultat(self, code: str, produit):
        if not produit:
            self._code_inconnu(code)
            return
        self._cibler_produit(produit)

    def _cibler_produit(self, produit: dict) -> None:
        """Amène la saisie de quantité sur la ligne du produit scanné."""
        item_id = self._ligne_du_produit(produit.get("id"))
        if not item_id:
            # Produit connu du serveur mais absent du comptage : archivé ou
            # créé depuis le démarrage de l'inventaire.
            self._scan_label.configure(
                text=t("« {nom} » n'est pas dans ce comptage (produit archivé "
                       "ou ajouté après le démarrage).").format(
                           nom=produit.get("name") or produit.get("sku") or "?"),
                text_color="orange")
            self.focus_scan()
            return

        info = self._comptages[item_id]
        # Un filtre de recherche actif détache la ligne : elle serait
        # introuvable et `see()` ne montrerait rien. Le scan prime sur le
        # filtre — on le lève plutôt que d'ignorer l'article scanné.
        query = self.search_var.get().strip().lower()
        if query and query not in info["sku"].lower() and query not in info["name"].lower():
            self.search_var.set("")
        self._scan_label.configure(
            text=t("{sku} — {nom}").format(sku=info["sku"], nom=info["name"]),
            text_color="#5fd08a")
        try:
            self.tree.see(item_id)
            self.tree.selection_set(item_id)
            self.tree.focus(item_id)
        except Exception:
            pass
        self._saisir_pour_item(item_id)
        self.focus_scan()

    def _code_inconnu(self, code: str) -> None:
        """Code sans produit : proposer de le rattacher à une fiche existante."""
        self._scan_label.configure(
            text=t("Code inconnu : {code}").format(code=code),
            text_color="orange")
        if not messagebox.askyesno(
            t("Code-barres inconnu"),
            t("Aucun produit ne porte le code « {code} ».\n\n"
              "Associer ce code à un produit existant maintenant ?\n\n"
              "« Non » : tu peux aussi le faire plus tard depuis l'écran "
              "Produits.").format(code=code),
        ):
            self.focus_scan()
            return

        def _associe(produit):
            # Le code est désormais enregistré : on reprend exactement là où
            # un scan réussi aurait mené.
            for p in self.products:
                if p.get("id") == produit.get("id"):
                    p["barcode"] = code
                    break
            self._cibler_produit(produit)

        AssocierCodeDialog(self, self.api, code, self.products,
                           on_associe=_associe)

    def actualiser_date(self):
        if not self._en_cours:
            self.date_var.set(datetime.now().strftime("%Y-%m-%d"))

    def refresh_products(self):
        """Précharge le catalogue à l'ouverture de l'onglet, sans figer l'UI.

        Appelée à chaque changement d'onglet : exécutée sur la boucle Tk, elle
        gelait toute l'application le temps du timeout réseau.
        """
        def _charger():
            try:
                produits = self.api.get_products()
                sauvegarder_produits(produits, sector=self.sector)
                return produits, None
            except ApiError as e:
                # Sans catalogue, l'inventaire ne peut même pas démarrer : on
                # retombe sur le dernier connu plutôt que de bloquer l'opérateur.
                # Étiqueté par secteur (local_cache.py) : un poste qui a servi
                # un autre secteur en dernier ne propose rien plutôt que le
                # mauvais catalogue.
                return charger_produits(sector=self.sector), e

        def _ok(resultat):
            if not self.winfo_exists():
                return
            # Un comptage déjà démarré travaille sur sa propre photo du
            # catalogue : ne pas la remplacer sous ses pieds.
            if self._en_cours:
                return
            produits, erreur = resultat
            if erreur is None:
                self.products = produits
                self.status_label.configure(text="")
            elif produits:
                self.products = produits
                self.status_label.configure(
                    text=t("Mode hors ligne — catalogue en cache"))
            else:
                self.status_label.configure(text=str(erreur))
            # Le catalogue est là : c'est le moment de proposer la reprise
            # d'un comptage interrompu, pas avant (la reprise a besoin des
            # produits pour reconstruire les lignes).
            self.proposer_reprise()

        def _err(exc):
            if self.winfo_exists():
                self.status_label.configure(text=str(exc))

        run_async(self, _charger, _ok, _err)

    def mettre_a_jour_operateur_defaut(self, ancien: str, nouveau: str):
        self.operateur_var.set(
            remplacer_operateur_defaut(self.operateur_var.get(), ancien, nouveau)
        )

    def _filtrer(self):
        if not self._en_cours:
            return
        query = self.search_var.get().strip().lower()
        for item_id, info in self._comptages.items():
            match = not query or query in info["sku"].lower() or query in info["name"].lower()
            if match:
                try:
                    self.tree.reattach(item_id, "", "end")
                except Exception:
                    pass
            else:
                self.tree.detach(item_id)

    def _demarrer(self):
        """Charge le catalogue en tâche de fond, puis ouvre la session.

        L'appel était fait en direct sur la boucle Tk : serveur lent ou
        injoignable, l'application entière restait figée jusqu'au timeout,
        sans le moindre signe. Même traitement que `refresh_products`.
        """
        if self._en_cours:
            return
        self.btn_demarrer.configure(state="disabled", text=t("Chargement..."))

        def _charger():
            try:
                produits = self.api.get_products()
                sauvegarder_produits(produits, sector=self.sector)
                return produits, None
            except ApiError as e:
                return charger_produits(sector=self.sector), e

        def _restaurer_bouton():
            if self.winfo_exists():
                self.btn_demarrer.configure(
                    state="normal", text=self._libelle_demarrer)

        def _ok(resultat):
            _restaurer_bouton()
            produits, erreur = resultat
            if erreur is not None and not produits:
                messagebox.showerror(
                    t("Erreur"),
                    t("Impossible de charger les produits :\n{e}").format(
                        e=erreur))
                return
            self.products = produits
            message = (t("Mode hors ligne — catalogue en cache")
                       if erreur is not None else "")
            # Un comptage interrompu doit être proposé AVANT d'ouvrir une
            # session vide : « Démarrer » est justement le geste de celui qui
            # revient après la coupure.
            if self.proposer_reprise(message):
                return
            self._ouvrir_session(message)

        def _err(exc):
            _restaurer_bouton()
            messagebox.showerror(t("Erreur"), str(exc))

        run_async(self, _charger, _ok, _err)

    def _ouvrir_session(self, message: str = ""):
        if not self.products:
            messagebox.showinfo(t("Inventaire"), t("Aucun produit en base."))
            return

        self._en_cours = True
        self._comptages.clear()
        self.tree.delete(*self.tree.get_children())
        self._cle_idempotence = str(uuid.uuid4())
        self.date_var.set(datetime.now().strftime("%Y-%m-%d"))

        for p in self.products:
            unite = p.get("unit", "pcs")
            item_id = self.tree.insert("", "end", tags=("non_compte",), values=(
                p["sku"], p["name"], "",
            ))
            self._comptages[item_id] = {
                # `item_id` est repris dans le dictionnaire : la seconde
                # saisie doit pouvoir remettre une ligne à « non compté »
                # sans reparcourir toute la table pour la retrouver.
                "item_id": item_id,
                "product_id": p["id"], "sku": p["sku"], "name": p["name"],
                "theorique": p["current_stock"], "compte": None,
                "unit": unite,
                # Rappel de la contenance d'un bidon : le comptage se saisit
                # toujours en unite de base, jamais en bidons.
                "bidon": (p.get("bidon_capacity")
                          if p.get("unit_type") == "volume" else None),
            }

        self.btn_demarrer.configure(state="disabled")
        self.btn_terminer.configure(state="normal")
        self.btn_annuler.configure(state="normal")
        self._sous_titre.configure(
            text=t("Double-clique sur la ligne pour saisir le niveau de la cuve.")
            if self._is_fuel else
            t("Double-clique sur une ligne pour saisir le comptage."))
        self._maj_compteur()
        # Le « Mode hors ligne — catalogue en cache » posé juste avant était
        # effacé ici même : l'opérateur comptait sur un catalogue périmé sans
        # jamais en être averti.
        self.status_label.configure(text=message)

    # ------------------------------------------- brouillon de comptage

    def _sauver_brouillon(self) -> None:
        """Écrit le comptage en cours sur le disque du poste.

        Appelé après CHAQUE quantité saisie plutôt que sur minuterie : c'est
        la seule façon de garantir qu'une coupure ne perd rien, et le coût
        (un petit fichier JSON réécrit de façon atomique) est sans commune
        mesure avec deux heures de comptage à refaire.
        """
        if not self._en_cours:
            return
        comptages = {str(v["product_id"]): v["compte"]
                     for v in self._comptages.values() if v["compte"] is not None}
        if not comptages:
            # Plus aucune quantité (tout a été remis à zéro par un refus de
            # seconde saisie) : le brouillon n'a plus rien à reprendre.
            effacer_comptage(CLE_COMPTAGE)
            return
        sauvegarder_comptage(CLE_COMPTAGE, {
            "operateur": self.operateur_var.get().strip(),
            "date": self.date_var.get(),
            # La clé d'idempotence est conservée : si la coupure est survenue
            # juste après l'envoi, la reprise renverra la MÊME clé et le
            # serveur reconnaîtra un doublon au lieu d'ajuster deux fois.
            "cle_idempotence": self._cle_idempotence,
            "comptages": comptages,
        }, sector=self.sector)

    def proposer_reprise(self, message: str = "") -> bool:
        """Propose de reprendre un comptage interrompu. Vrai s'il a été repris."""
        if self._en_cours or self._reprise_proposee or not self.products:
            return False
        self._reprise_proposee = True
        brouillon = charger_comptage(CLE_COMPTAGE, sector=self.sector)
        if not brouillon:
            return False

        nb = len(brouillon.get("comptages") or {})
        auteur = (brouillon.get("operateur") or brouillon.get("username")
                  or "").strip()
        quand = (brouillon.get("created_at") or "").replace("T", " ")
        detail = t("Commencé le {quand}").format(quand=quand) if quand else ""
        if auteur:
            detail += t(" par {qui}").format(qui=auteur)

        if not messagebox.askyesno(
            t("Comptage interrompu"),
            t("Un inventaire n'a pas été terminé : {n} produit(s) déjà "
              "comptés.\n{detail}\n\nReprendre ce comptage ?\n\n"
              "« Non » efface ces saisies et repart d'un comptage vide.").format(
                  n=nb, detail=detail),
        ):
            effacer_comptage(CLE_COMPTAGE)
            return False

        self._reprendre(brouillon, message)
        return True

    def _reprendre(self, brouillon: dict, message_session: str = "") -> None:
        """Rouvre une session et y replace les quantités déjà saisies."""
        self._ouvrir_session(message_session)
        if not self._en_cours:
            return

        comptages = brouillon.get("comptages") or {}
        if brouillon.get("operateur"):
            self.operateur_var.set(brouillon["operateur"])
        if brouillon.get("cle_idempotence"):
            self._cle_idempotence = brouillon["cle_idempotence"]

        restaures = 0
        for item_id, info in self._comptages.items():
            valeur = comptages.get(str(info["product_id"]))
            if valeur is None:
                continue
            try:
                info["compte"] = float(valeur)
            except (TypeError, ValueError):
                info["compte"] = None
                continue
            unite = info.get("unit") or "pcs"
            self.tree.item(item_id, tags=("compte",), values=(
                info["sku"], info["name"], f"{info['compte']:g} {unite}",
            ))
            restaures += 1

        self._maj_compteur()
        perdus = len(comptages) - restaures
        message = t("Comptage repris : {n} quantité(s) restaurée(s).").format(
            n=restaures)
        if message_session:
            message = message_session + " — " + message
        if perdus > 0:
            # Un produit archivé (ou supprimé du catalogue) depuis la coupure
            # n'a plus de ligne : le dire, plutôt que de laisser croire que
            # tout a été retrouvé.
            message += " " + t("{n} produit(s) du brouillon ne sont plus au "
                               "catalogue.").format(n=perdus)
        self.status_label.configure(text=message)
        # Le brouillon est réécrit sur la base de ce qui a réellement été
        # restauré : sinon un produit disparu resterait à jamais dans le
        # fichier et fausserait le compteur de la prochaine reprise.
        self._sauver_brouillon()

    def _saisir_comptage(self, event):
        if not self._en_cours:
            return
        item_id = self.tree.identify_row(event.y)
        if not item_id or item_id not in self._comptages:
            return
        self._saisir_pour_item(item_id)

    def _saisir_pour_item(self, item_id: str) -> None:
        """Ouvre la saisie de quantité d'une ligne.

        Extrait de `_saisir_comptage` pour que le scan mène au MÊME dialogue
        que le double-clic : une seule façon de saisir une quantité, donc un
        seul endroit où les contrôles (nombre positif, brouillon) s'appliquent.
        """
        if not self._en_cours or item_id not in self._comptages:
            return

        info = self._comptages[item_id]
        unite = info.get("unit") or "pcs"
        rappel = ""
        if info.get("bidon"):
            rappel = "\n" + t("(1 bidon = {cap:g} {unite})").format(
                cap=info["bidon"], unite=unite)
        dialogue = ctk.CTkInputDialog(
            title=t("Relevé de jauge") if self._is_fuel else t("Comptage"),
            text=(t("{sku} — {nom}\n\nNiveau relevé (en {unite}) :").format(
                      sku=info["sku"], nom=info["name"], unite=unite)
                  if self._is_fuel else
                  t("{sku} — {nom}{rappel}\n\nQuantité comptée "
                    "(en {unite}) :").format(
                        sku=info["sku"], nom=info["name"], rappel=rappel,
                        unite=unite)),
        )
        reponse = dialogue.get_input()
        if reponse is None:
            return
        reponse = reponse.strip()
        if not reponse:
            return
        try:
            compte = float_saisie(reponse)
            if compte < 0:
                raise ValueError
        except ValueError:
            messagebox.showwarning(t("Valeur invalide"),
                                   t("Saisis un nombre positif ou zéro."))
            return

        info["compte"] = compte
        self.tree.item(item_id, tags=("compte",), values=(
            info["sku"], info["name"], f"{compte:g} {unite}",
        ))
        self._maj_compteur()
        self._sauver_brouillon()

    def _maj_compteur(self):
        total = len(self._comptages)
        comptes = sum(1 for c in self._comptages.values() if c["compte"] is not None)
        self._compteur_label.configure(
            text=t("{comptes} / {total} produits comptés").format(
                comptes=comptes, total=total))

    def _annuler_inventaire(self):
        if messagebox.askyesno(
                t("Annuler l'inventaire"),
                t("Abandonner cet inventaire en cours ?\n"
                  "Les comptages saisis seront perdus.")):
            self._reinitialiser()

    def _reinitialiser(self):
        # La session est close (envoyée, mise en file d'attente ou abandonnée
        # volontairement) : le brouillon n'a plus lieu d'être. Il n'est effacé
        # QUE par ce chemin — jamais à la fermeture de l'écran, qui est
        # justement l'accident contre lequel il protège.
        effacer_comptage(CLE_COMPTAGE)
        self._reprise_proposee = True
        self._en_cours = False
        self._comptages.clear()
        self.tree.delete(*self.tree.get_children())
        self.btn_demarrer.configure(state="normal")
        # Le libellé doit revenir à sa forme normale : `_terminer` l'a passé à
        # « Envoi en cours... » et rien ne le remettait ici. Après un premier
        # inventaire réussi (ou mis en file d'attente), le bouton gardait ce
        # texte pour toute la suite de la session, y compris pendant
        # l'inventaire suivant.
        self.btn_terminer.configure(state="disabled", text=self._libelle_terminer)
        self.btn_annuler.configure(state="disabled")
        self._compteur_label.configure(text="")
        self._sous_titre.configure(
            text=t("Relève le niveau de la cuve — matin avant les livraisons, "
                   "soir avant la fermeture.")
            if self._is_fuel else
            t("Lance un inventaire pour compter tous les produits de "
              "l'entrepôt."))
        self.status_label.configure(text="")

    # WebSocket methods for real-time updates
    def maj_stock_produit(self, product_id, new_stock):
        """Mettre à jour le stock théorique d'un produit."""
        if self._en_cours:
            # Ne pas mettre à jour le stock théorique pendant un inventaire en cours
            # pour éviter de confondre l'opérateur.
            return
        # Mettre à jour le produit dans la liste self.products
        for product in self.products:
            if product["id"] == product_id:
                product["current_stock"] = new_stock
                break
        # Note: Nous ne rafraîchissons pas l'affichage car l'affichage des produits
        # n'est montré que pendant l'inventaire, et nous ne voulons pas le perturber.

    def _terminer(self):
        comptes = {k: v for k, v in self._comptages.items() if v["compte"] is not None}
        non_comptes = len(self._comptages) - len(comptes)

        if not comptes:
            messagebox.showinfo(t("Inventaire"),
                                t("Aucun produit n'a été compté."))
            return

        if non_comptes > 0:
            if not messagebox.askyesno(
                t("Produits non comptés"),
                t("{n} produit(s) n'ont pas été comptés.\n\nLes terminer quand "
                  "même ? Seuls les produits comptés seront ajustés.").format(
                      n=non_comptes),
            ):
                return

        ecarts = []
        for v in comptes.values():
            delta = v["compte"] - v["theorique"]
            if abs(delta) > 1e-9:
                ecarts.append(v)

        date_label = datetime.now().strftime("%d/%m/%Y")
        # NE PAS traduire : cette chaîne est enregistrée en base comme motif
        # de l'ajustement, et le filtre « Inventaires » de l'historique la
        # reconnaît par son préfixe (reference_prefix="Inventaire complet").
        motif = f"Inventaire complet du {date_label}"

        if not ecarts:
            # Un comptage CONFORME reste une information d'exploitation :
            # qui a compté, quand, et combien — pas seulement un écart à
            # corriger. Envoyer quand même (le serveur sait déjà l'accepter,
            # voir `releve_conforme` dans /adjustments) plutôt que de
            # disparaître sans trace : sinon rien ne prouve que l'inventaire
            # a eu lieu, et il n'apparaît nulle part dans l'Historique —
            # constaté en réel le 13 septembre 2026 (secteur RAN), même bug
            # que celui déjà signalé pour le jaugeage Fuel le 2026-09-08.
            if not messagebox.askyesno(
                t("Confirmer l'inventaire"),
                t("{n} produit(s) comptés — aucun écart constaté.\n\nLe stock "
                  "correspond au comptage physique. Enregistrer quand même "
                  "ce relevé dans l'historique ?").format(n=len(comptes)),
            ):
                self._reinitialiser()
                return
            lines = [{"product_id": v["product_id"], "counted_quantity": v["compte"]}
                     for v in comptes.values()]
            self.btn_terminer.configure(state="disabled", text=t("Envoi en cours..."))
            self.btn_annuler.configure(state="disabled")
            self._envoyer_ajustement(motif, lines, comptes, ecarts, non_comptes,
                                     confirmation=False)
            return

        # Affiché en valeur absolue : « Manquant : -5 » à côté de
        # « Surplus : +5 » se lit de travers.
        manques = -sum(v["compte"] - v["theorique"] for v in ecarts
                       if v["compte"] < v["theorique"])
        surplus = sum(v["compte"] - v["theorique"] for v in ecarts
                      if v["compte"] > v["theorique"])

        est_releve_seul = self._is_fuel and self._role != "admin"
        if est_releve_seul:
            msg_fin = t("Le relevé sera enregistré (stock non corrigé — "
                        "seul l'administrateur peut corriger le stock). "
                        "Continuer ?")
        else:
            msg_fin = t("Le stock sera corrigé. Continuer ?")
        if not messagebox.askyesno(
            t("Confirmer l'inventaire"),
            t("{n} produit(s) comptés, {ecarts} avec écart.\n\n"
              "Manquant : {manques:g}\nSurplus : +{surplus:g}\n\n"
              "{action}").format(
                  n=len(comptes), ecarts=len(ecarts), manques=manques,
                  surplus=surplus, action=msg_fin),
        ):
            return

        # Écart anormalement élevé : la quantité doit être retapée à l'aveugle
        # AVANT l'envoi. Faire ce contrôle ici plutôt qu'en réaction au refus
        # du serveur évite de faire retaper l'opérateur après un aller-retour
        # réseau — et laisse la session intacte s'il doit aller recompter.
        importants = ecarts_a_re_saisir(ecarts)
        if importants and not self._double_saisie(importants):
            return

        lines = [{"product_id": v["product_id"], "counted_quantity": v["compte"]}
                 for v in comptes.values()]

        self.btn_terminer.configure(state="disabled", text=t("Envoi en cours..."))
        self.btn_annuler.configure(state="disabled")

        self._envoyer_ajustement(motif, lines, comptes, ecarts, non_comptes,
                                 confirmation=bool(importants))

    def _double_saisie(self, entrees) -> bool:
        """Fait retaper chaque quantité à écart important, à l'aveugle.

        Renvoie True si TOUTES les secondes saisies concordent. Sinon, la
        quantité fautive est remise à « non compté » et l'inventaire n'est pas
        envoyé : l'opérateur doit retourner compter physiquement le produit.
        Le premier chiffre n'apparaît nulle part dans le dialogue — sans quoi
        la seconde saisie ne serait qu'une recopie.
        """
        for info in entrees:
            unite = info.get("unit") or "pcs"
            dialogue = ctk.CTkInputDialog(
                title=t("Vérification obligatoire"),
                text=t("{sku} — {nom}\n\nL'écart avec le stock théorique est "
                       "anormalement élevé.\nRetape la quantité comptée "
                       "(en {unite}) sans regarder ta première saisie :").format(
                           sku=info["sku"], nom=info["name"], unite=unite),
            )
            reponse = dialogue.get_input()
            if reponse is None or not str(reponse).strip():
                messagebox.showinfo(
                    t("Vérification abandonnée"),
                    t("L'inventaire n'a pas été envoyé : la quantité de "
                      "« {nom} » n'a pas été confirmée.").format(
                          nom=info["name"]))
                return False
            try:
                seconde = float_saisie(reponse)
            except ValueError:
                seconde = None
            if seconde is None or not saisies_concordent(info["compte"], seconde):
                self._invalider_comptage(info)
                messagebox.showerror(
                    t("Les deux saisies ne concordent pas"),
                    t("{sku} — {nom}\n\nLa quantité retapée ne correspond pas "
                      "à la première saisie.\n\nRetourne compter physiquement "
                      "ce produit, puis saisis à nouveau sa quantité.\n\n"
                      "L'inventaire n'a PAS été envoyé.").format(
                          sku=info["sku"], nom=info["name"]))
                return False
        return True

    def _invalider_comptage(self, info: dict) -> None:
        """Remet une ligne à « non compté » après une seconde saisie discordante."""
        info["compte"] = None
        item_id = info.get("item_id")
        if item_id:
            try:
                self.tree.item(item_id, tags=("non_compte",), values=(
                    info["sku"], info["name"], "",
                ))
            except Exception:
                pass
        self._maj_compteur()
        self._sauver_brouillon()

    def _envoyer_ajustement(self, motif, lines, comptes, ecarts, non_comptes,
                            confirmation: bool):
        """Lance l'appel API en arriere-plan pour ne pas bloquer l'interface."""
        def appel():
            return self.api.create_adjustment(
                reason=motif,
                lines=lines,
                operator=self.operateur_var.get().strip(),
                region="",
                created_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                idempotency_key=self._cle_idempotence,
                created_by=self.operateur_var.get().strip(),
                confirmation_ecart_important=confirmation,
            )

        def succes(resultat):
            self._afficher_rapport(resultat, comptes, ecarts, non_comptes)
            self._reinitialiser()

        def erreur(exc):
            err = str(exc)
            if not confirmation and "confirmation requise" in err.lower():
                # Filet de sécurité : le contrôle a normalement déjà eu lieu
                # avant l'envoi (`_terminer`). On n'arrive ici que si le
                # serveur applique un seuil que ce poste ne connaît pas encore
                # (version plus récente). La réponse reste la même : retaper,
                # jamais un simple « oui ».
                self._reactiver_boutons()
                messagebox.showwarning(t("Écart important détecté"), err)
                a_verifier = ecarts_a_re_saisir(list(comptes.values())) or ecarts
                if not self._double_saisie(a_verifier):
                    return
                self._cle_idempotence = str(uuid.uuid4())
                self.btn_terminer.configure(state="disabled",
                                            text=t("Envoi en cours..."))
                self.btn_annuler.configure(state="disabled")
                self._envoyer_ajustement(motif, lines, comptes, ecarts,
                                         non_comptes, confirmation=True)
                return
            if "contacter le serveur" in err.lower() or "timeout" in err.lower():
                if messagebox.askyesno(
                    t("Serveur injoignable"),
                    t("Le serveur ne répond pas.\n\n"
                      "Mettre cet inventaire en file d'attente ?"),
                ):
                    payload = {
                        "reason": motif, "lines": lines,
                        "operator": self.operateur_var.get().strip(),
                        "region": "",
                        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "idempotency_key": self._cle_idempotence,
                        "created_by": self.operateur_var.get().strip(),
                    }
                    mettre_en_attente("adjustment", payload, username=self.api.username)
                    if self._on_queue_change:
                        self._on_queue_change()
                    self._reinitialiser()
                    return
                self._reactiver_boutons()
                return
            messagebox.showerror(t("Erreur"), err)
            self._reactiver_boutons()

        run_async(self, appel, succes, erreur)

    def _reactiver_boutons(self):
        """Retablit les boutons apres un echec d'envoi."""
        self.btn_terminer.configure(state="normal", text=self._libelle_terminer)
        self.btn_annuler.configure(state="normal")

    def _indicateur_ampleur(self, v: dict) -> str:
        """Qualifie un écart : dans la marge normale, ou à surveiller.

        Même seuil que la double saisie à l'aveugle (`seuil_ecart_important`,
        partagé avec le serveur) : un écart en-dessous n'a jamais forcé de
        re-saisie, donc n'a jamais été mis en doute. Pour une cuve, cette
        marge couvre l'imprécision de jauge normale et l'évaporation — le
        rapport le dit plutôt que de laisser croire qu'un écart de quelques
        gallons est une anomalie.
        """
        delta = v["compte"] - v["theorique"]
        seuil = seuil_ecart_important(v["theorique"])
        if abs(delta) <= seuil:
            return (t("écart normal — jauge/évaporation") if self._is_fuel
                    else t("écart normal"))
        return t("⚠ écart important — vérifié en double saisie")

    def _afficher_rapport(self, resultat, comptes, ecarts, non_comptes):
        lignes_rapport = []
        lignes_rapport.append(
            (t("Jaugeage n°{id} enregistré") if self._is_fuel else
             t("Inventaire n°{id} enregistré")).format(id=resultat["id"]))
        lignes_rapport.append(t("Date : {date}").format(
            date=datetime.now().strftime("%d/%m/%Y %H:%M")))
        lignes_rapport.append(t("Opérateur : {nom}").format(
            nom=self.operateur_var.get().strip() or "—"))
        lignes_rapport.append("")
        lignes_rapport.append(
            (t("Cuves relevées : {n}") if self._is_fuel else
             t("Produits comptés : {n}")).format(n=len(comptes)))
        lignes_rapport.append(
            (t("Cuves non relevées : {n}") if self._is_fuel else
             t("Produits non comptés : {n}")).format(n=non_comptes))
        lignes_rapport.append(t("Écarts constatés : {n}").format(n=len(ecarts)))
        lignes_rapport.append("")

        if ecarts:
            manques = [v for v in ecarts if v["compte"] < v["theorique"]]
            surplus_list = [v for v in ecarts if v["compte"] > v["theorique"]]

            if manques:
                titre = (t("NIVEAU EN BAISSE ({n}) :") if self._is_fuel else
                          t("MANQUES ({n}) :"))
                lignes_rapport.append(titre.format(n=len(manques)))
                for v in manques:
                    delta = v["compte"] - v["theorique"]
                    u = v.get("unit", "pcs")
                    lignes_rapport.append(
                        f"  {v['sku']} — {v['name']} : "
                        f"{v['theorique']:g} → {v['compte']:g} {u} ({delta:+g}) "
                        f"— {self._indicateur_ampleur(v)}")
                lignes_rapport.append("")

            if surplus_list:
                titre = (t("NIVEAU EN HAUSSE ({n}) :") if self._is_fuel else
                          t("SURPLUS ({n}) :"))
                lignes_rapport.append(titre.format(n=len(surplus_list)))
                for v in surplus_list:
                    delta = v["compte"] - v["theorique"]
                    u = v.get("unit", "pcs")
                    lignes_rapport.append(
                        f"  {v['sku']} — {v['name']} : "
                        f"{v['theorique']:g} → {v['compte']:g} {u} ({delta:+g}) "
                        f"— {self._indicateur_ampleur(v)}")

        # Un ajustement peut faire descendre un produit sous son seuil mini :
        # le serveur le signale, on le remonte dans le rapport final.
        alertes = (resultat or {}).get("alertes_seuil") or []
        if alertes:
            lignes_rapport.append("")
            lignes_rapport.append(t("ALERTES SEUIL ({n}) :").format(n=len(alertes)))
            for a in alertes:
                lignes_rapport.append(
                    f"  ⚠ {a['sku']} — {a['name']} : "
                    + t("stock {stock:g} / seuil {seuil:g}").format(
                        stock=a["stock"], seuil=a["seuil"]))

        rapport = "\n".join(lignes_rapport)

        titre_rapport = t("Rapport de jaugeage") if self._is_fuel else t("Rapport d'inventaire")

        fenetre = ctk.CTkToplevel(self)
        fenetre.title(titre_rapport)
        fenetre.geometry("600x500")
        fenetre.transient(self.winfo_toplevel())
        fenetre.lift()
        fenetre.attributes("-topmost", True)
        fenetre.after(200, lambda: fenetre.attributes("-topmost", False))
        fenetre.focus_force()
        fenetre.grab_set()

        ctk.CTkLabel(fenetre, text=titre_rapport,
                     font=ctk.CTkFont(size=18, weight="bold")).pack(pady=(16, 8))

        zone_texte = ctk.CTkTextbox(fenetre, width=560, height=360)
        zone_texte.pack(padx=16, pady=8)
        zone_texte.insert("1.0", rapport)
        zone_texte.configure(state="disabled")

        barre_btn = ctk.CTkFrame(fenetre, fg_color="transparent")
        barre_btn.pack(pady=8)

        ctk.CTkButton(
            barre_btn, text=t("Exporter Excel"), width=160,
            fg_color="#1a6b3a", hover_color="#228b4a",
            command=lambda: self._exporter_rapport_excel(fenetre, resultat, comptes, ecarts, non_comptes),
        ).pack(side="left", padx=8)
        ctk.CTkButton(barre_btn, text=t("Fermer"), width=100,
                       fg_color="gray30", command=fenetre.destroy).pack(side="left", padx=8)

    def _exporter_rapport_excel(self, fenetre_rapport, resultat, comptes, ecarts, non_comptes):
        import os
        from pathlib import Path

        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill

        bureau = Path(os.path.expanduser("~/Desktop"))
        if not bureau.exists():
            bureau = Path(os.path.expanduser("~/Documents"))

        date_label = datetime.now().strftime("%Y-%m-%d")
        titre_rapport = t("Rapport de jaugeage") if self._is_fuel else t("Rapport d'inventaire")

        try:
            fenetre_rapport.grab_release()
        except Exception:
            pass

        prefixe_fichier = "jaugeage" if self._is_fuel else "inventaire"
        chemin = filedialog.asksaveasfilename(
            parent=fenetre_rapport,
            defaultextension=".xlsx",
            filetypes=[("Excel", "*.xlsx")],
            initialfile=f"{prefixe_fichier}_{date_label}.xlsx",
            initialdir=str(bureau),
        )
        if not chemin:
            try:
                fenetre_rapport.grab_set()
            except Exception:
                pass
            return

        wb = Workbook()
        ws = wb.active
        ws.title = titre_rapport[:31]

        gras = Font(bold=True)
        rouge = Font(bold=True, color="CC0000")
        vert = Font(bold=True, color="008800")
        orange = Font(bold=True, color="B45309")
        gris = PatternFill(start_color="F2F2F2", end_color="F2F2F2", fill_type="solid")

        ws.append([titre_rapport])
        ws["A1"].font = Font(bold=True, size=14)
        ws.append([t("Date : {date}").format(
            date=datetime.now().strftime("%d/%m/%Y %H:%M"))])
        ws.append([t("Opérateur : {nom}").format(
            nom=self.operateur_var.get().strip() or "—")])
        ws.append([t("N° document : {id}").format(id=resultat["id"])])
        ws.append([])
        libelle_comptees = t("Cuves relevées : {n}") if self._is_fuel else t("Produits comptés : {n}")
        libelle_non_comptees = t("Cuves non relevées : {n}") if self._is_fuel else t("Non comptés : {n}")
        ws.append([libelle_comptees.format(n=len(comptes)),
                   libelle_non_comptees.format(n=non_comptes),
                   t("Écarts : {n}").format(n=len(ecarts))])
        ws.append([])

        libelle_theorique = t("Niveau théorique") if self._is_fuel else t("Stock théorique")
        libelle_compte = t("Niveau relevé") if self._is_fuel else t("Compté")
        entetes = ["SKU", t("Produit"), t("Unité"), libelle_theorique,
                   libelle_compte, t("Écart"), t("Statut"), t("Ampleur")]
        ws.append(entetes)
        for cell in ws[ws.max_row]:
            cell.font = gras
            cell.fill = gris

        for v in comptes.values():
            delta = v["compte"] - v["theorique"]
            if abs(delta) < 1e-9:
                statut = t("OK")
            elif delta < 0:
                statut = t("Manque")
            else:
                statut = t("Surplus")
            ampleur = self._indicateur_ampleur(v) if abs(delta) >= 1e-9 else ""
            ws.append([v["sku"], v["name"], v.get("unit", "pcs"), v["theorique"],
                        v["compte"], round(delta, 3), statut, ampleur])
            ligne = ws.max_row
            if statut == t("Manque"):
                ws.cell(ligne, 6).font = rouge
                ws.cell(ligne, 7).font = rouge
            elif statut == t("Surplus"):
                ws.cell(ligne, 6).font = vert
                ws.cell(ligne, 7).font = vert
            if ampleur.startswith("⚠"):
                ws.cell(ligne, 8).font = orange

        for col in ws.columns:
            largeur = max(len(str(c.value or "")) for c in col) + 2
            ws.column_dimensions[col[0].column_letter].width = min(largeur, 40)

        try:
            wb.save(chemin)
            messagebox.showinfo(t("Export"),
                                t("Rapport exporté vers :\n{path}").format(path=chemin))
        except OSError as e:
            messagebox.showerror(
                t("Erreur"),
                t("Impossible d'enregistrer :\n{e}").format(e=e))


class AssocierCodeDialog(ctk.CTkToplevel):
    """Rattache un code-barres inconnu à un produit existant, sans quitter
    l'inventaire.

    C'est le geste naturel du terrain : on scanne un article, il n'a pas
    encore de code, on l'associe une fois pour toutes et on continue de
    compter. Renvoyer l'opérateur vers l'écran Produits (réservé à l'admin,
    et à l'autre bout de l'application) reviendrait à ne jamais rien coder.

    Aucune création de fiche ici, volontairement : un code inconnu veut dire
    « quel article est-ce ? », jamais « crée un nouvel article » — un doublon
    créé au vol couperait un stock en deux.
    """

    def __init__(self, master, api, code: str, produits: list[dict], on_associe=None):
        super().__init__(master)
        self.api = api
        self.code = code
        self.on_associe = on_associe
        # Un produit archivé n'est pas dans le comptage : le proposer ne
        # mènerait qu'à « n'est pas dans ce comptage » au retour.
        self._produits = [p for p in (produits or []) if not p.get("archived")]
        self._resultats: list[dict] = []

        self.title(t("Associer le code-barres"))
        self.geometry("560x460")
        self.transient(master.winfo_toplevel())
        self.lift()
        self.grab_set()

        ctk.CTkLabel(self, text=t("Code scanné : {code}").format(code=code),
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(16, 2))
        ctk.CTkLabel(self,
                     text=t("Choisis le produit qui porte cette étiquette. Le "
                            "code lui sera enregistré définitivement."),
                     text_color="#9ca3af", wraplength=500).pack(pady=(0, 10))

        self.recherche_var = tk.StringVar()
        self.recherche_var.trace_add("write", lambda *_: self._rendre())
        entree = ctk.CTkEntry(self, textvariable=self.recherche_var,
                              placeholder_text=t("🔍  Chercher un produit (SKU ou nom)…"),
                              width=480, height=32)
        entree.pack(padx=20)
        entree.focus_set()

        cadre = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=8)
        cadre.pack(fill="both", expand=True, padx=20, pady=10)
        self.liste = ttk.Treeview(cadre, columns=("sku", "nom"),
                                  show="headings", height=10)
        self.liste.heading("sku", text="SKU")
        self.liste.heading("nom", text=t("Produit"))
        self.liste.column("sku", width=140, anchor="w")
        self.liste.column("nom", width=340, anchor="w")
        self.liste.pack(side="left", fill="both", expand=True)
        barre = ttk.Scrollbar(cadre, orient="vertical", command=self.liste.yview)
        self.liste.configure(yscrollcommand=barre.set)
        barre.pack(side="right", fill="y")
        self.liste.bind("<Double-1>", lambda _e: self._associer())

        self.status_lbl = ctk.CTkLabel(self, text="", text_color="orange")
        self.status_lbl.pack()

        boutons = ctk.CTkFrame(self, fg_color="transparent")
        boutons.pack(pady=(4, 14))
        ctk.CTkButton(boutons, text=t("Annuler"), width=110,
                      fg_color="#2a2d35", hover_color="#353840",
                      command=self.destroy).pack(side="left", padx=8)
        self._bouton = ctk.CTkButton(boutons, text=t("Associer"), width=150,
                                     fg_color="#2563eb", hover_color="#1d4ed8",
                                     command=self._associer)
        self._bouton.pack(side="left", padx=8)

        self._rendre()

    def resultats(self, requete: str) -> list[dict]:
        """Produits correspondant à la recherche (recherche floue partagée).

        Bornée à 200 lignes : au-delà, la liste ne s'utilise plus, on tape
        deux lettres de plus. Méthode publique parce qu'elle porte toute la
        logique de l'écran et se teste sans ouvrir de fenêtre.
        """
        return filtrer(self._produits, requete)[:200]

    def _rendre(self):
        requete = self.recherche_var.get()
        correspondances = filtrer(self._produits, requete)
        self._resultats = correspondances[:200]
        self.liste.delete(*self.liste.get_children())
        for p in self._resultats:
            self.liste.insert("", "end", values=(p.get("sku", ""), p.get("name", "")))
        # Repéré par audit du 9 septembre 2026 : la troncature à 200 était
        # muette — un produit réellement présent mais recalé au-delà de la
        # 200e ligne semblait ne pas exister du tout. `status_lbl` sert déjà
        # aux messages d'erreur de `_associer()` (rien en conflit : celui-ci
        # ne s'affiche qu'au clic, celui-ci à chaque frappe).
        if len(correspondances) > len(self._resultats):
            self.status_lbl.configure(
                text=t("{n} résultats affichés sur {total} — affine ta "
                       "recherche pour voir les autres.").format(
                           n=len(self._resultats), total=len(correspondances)),
                text_color="#9ca3af")
        else:
            self.status_lbl.configure(text="")

    def _produit_selectionne(self):
        selection = self.liste.selection()
        if not selection:
            return None
        index = self.liste.index(selection[0])
        if 0 <= index < len(self._resultats):
            return self._resultats[index]
        return None

    def _associer(self):
        produit = self._produit_selectionne()
        if not produit:
            self.status_lbl.configure(text=t("Choisis d'abord un produit dans la liste."))
            return
        self._bouton.configure(state="disabled", text=t("Enregistrement…"))

        def _ok(_res):
            if self.on_associe:
                self.on_associe(produit)
            if self.winfo_exists():
                self.destroy()

        def _err(exc):
            if self.winfo_exists():
                self._bouton.configure(state="normal", text=t("Associer"))
                # Message du serveur tel quel : il nomme le produit qui porte
                # déjà ce code (409), ce qui est exactement l'information utile.
                self.status_lbl.configure(text=str(exc))

        run_async(self,
                  lambda: self.api.update_product(produit["id"], barcode=self.code),
                  _ok, _err)
