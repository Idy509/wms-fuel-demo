"""Indicateurs de pilotage — écran d'administration.

Onze mesures qu'aucun autre écran ne donne, chacune sur sa propre section
pour ne pas alourdir le tableau de bord d'accueil, déjà dense :

  1. Précision d'inventaire — écart entre comptage et théorique, dernier cycle.
  2. Concentration régions — part de chaque région dans le volume livré.
  3. Comptages à temps — régions ayant rendu leur comptage ce mois-ci.
  4. Anomalies du mois — mouvements très éloignés de l'habitude du produit.
  5. Consommation régionale — sorties par région et par produit, sur 12 mois.
  6. Produits dormants — ce qui ne sort plus, et l'argent que ça immobilise.
  7. Résolution des alertes — délai de réponse du central aux ruptures signalées.
  8. Dette de consigne — bouteilles consignées non rendues, par détenteur.
  9. Activité opérateurs — qui saisit combien de bons, et à quelle heure.
 10. Écarts de réception — ce qui part du central et n'arrive jamais.
 11. Régularité fournisseurs — à quel rythme chaque fournisseur livre.

Écran de lecture seule : rien n'y est modifiable. Les appels serveur sont
indépendants et lancés en parallèle par `refresh()` — une section en erreur
(par exemple un serveur d'ancienne version qui ne connaît pas encore
l'endpoint) affiche son message sans empêcher les autres de s'afficher.

Réservé au rôle administrateur : le serveur refuse ces endpoints aux autres
rôles, et la barre latérale n'affiche pas l'entrée.
"""
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from async_call import run_async
from i18n import t
from views.utils import sort_treeview as _sort_treeview

# Sections, dans l'ordre du sélecteur. Les clés servent d'identifiant interne
# et ne sont jamais affichées : c'est le libellé traduit qui l'est.
SECTIONS = (
    ("precision", "Précision d'inventaire"),
    ("regions", "Concentration régions"),
    ("comptages", "Comptages à temps"),
    ("anomalies", "Anomalies du mois"),
    ("consommation", "Consommation régionale"),
    ("dormants", "Produits dormants"),
    ("alertes", "Résolution des alertes"),
    ("consigne", "Dette de consigne"),
    ("operateurs", "Activité opérateurs"),
    ("ecarts", "Écarts de réception"),
    ("fournisseurs", "Régularité fournisseurs"),
)

# Sections sans objet pour le secteur FUEL : quatre reposent sur les RÉGIONS
# (Fuel s'organise par site, pas par région). Le serveur les renvoie désormais
# vides pour ce secteur — les afficher quand même donnerait quatre onglets
# définitivement muets, et laisserait croire à une panne plutôt qu'à une
# absence d'objet.
SECTIONS_SANS_OBJET_FUEL = frozenset({
    "regions", "comptages", "consommation", "alertes",
})

# La consigne bouteille est propre à Consumables (personne d'autre n'en a) :
# masquée pour tous les autres secteurs, pas seulement Fuel — sinon FON et
# RAN gardaient un onglet « Dette de consigne » perpétuellement vide.
SECTIONS_SANS_OBJET_HORS_CONSUMABLES = frozenset({"consigne"})

FOND = "#1a1d23"
BORDURE = "#2a2d35"
GRIS = "#6b7280"
GRIS_CLAIR = "#9ca3af"
VERT = "#4ade80"
ORANGE = "#f59e0b"
ROUGE = "#ef4444"


def _fmt(valeur) -> str:
    """Nombre lisible : espace fine pour les milliers, virgule décimale."""
    if valeur is None:
        return "—"
    try:
        nombre = float(valeur)
    except (TypeError, ValueError):
        return str(valeur)
    texte = f"{nombre:,.2f}".rstrip("0").rstrip(".") if nombre % 1 else f"{nombre:,.0f}"
    return texte.replace(",", " ").replace(".", ",")


def _pct(valeur) -> str:
    return "—" if valeur is None else f"{_fmt(valeur)} %"


def _couleur_precision(valeur) -> str:
    """Seuils usuels d'un entrepôt : 95 % est la cible, 90 % le plancher."""
    if valeur is None:
        return GRIS
    if valeur >= 95:
        return VERT
    if valeur >= 90:
        return ORANGE
    return ROUGE


class IndicateursView(ctk.CTkFrame):

    def __init__(self, master, api, user_role=None, sector=None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self.user_role = user_role
        self.sector = sector
        # Sections retenues pour CE secteur : quatre reposent sur les régions
        # (Fuel n'en a pas) et une sur la consigne bouteille (Consumables
        # seul en a). Sans ce filtre, ces onglets ne diraient jamais rien,
        # au milieu de ceux qui comptent.
        self._sections_visibles = tuple(
            (code, libelle) for code, libelle in SECTIONS
            if not (sector == "FUEL" and code in SECTIONS_SANS_OBJET_FUEL)
            and not (sector != "CONSUMABLES" and code in SECTIONS_SANS_OBJET_HORS_CONSUMABLES))
        self._sections: dict[str, ctk.CTkFrame] = {}
        self._libelles = {t(libelle): code for code, libelle in self._sections_visibles}
        self._build()
        self._afficher_section(t(self._sections_visibles[0][1]))

    # ----------------------------------------------------------- interface

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 4))
        ctk.CTkLabel(top, text=t("📈  Indicateurs de pilotage"),
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")
        ctk.CTkButton(top, text=t("↻  Rafraîchir"), width=110,
                      fg_color=BORDURE, hover_color="#353840",
                      corner_radius=8, command=self.refresh).pack(side="right")

        # Deux actions d'administration greffées ici plutôt que sur le tableau
        # de bord d'accueil : l'export comptable est le seul export qui montre
        # les coûts d'achat, et le rapport par e-mail part vers la direction.
        # Ni l'un ni l'autre n'a sa place sur un écran que tout le monde
        # ouvre. La barre latérale masque déjà cet écran aux non-admins ; le
        # test ci-dessous n'est qu'une ceinture de plus, comme ailleurs.
        if self.user_role == "admin":
            self.btn_rapport_email = ctk.CTkButton(
                top, text=t("✉️  Envoyer le rapport mensuel maintenant"), width=280,
                fg_color=BORDURE, hover_color="#353840", corner_radius=8,
                command=self._envoyer_rapport_mensuel)
            self.btn_rapport_email.pack(side="right", padx=(0, 8))

            self.btn_export_comptable = ctk.CTkButton(
                top, text=t("📊  Export comptable"), width=170,
                fg_color=BORDURE, hover_color="#353840", corner_radius=8,
                command=self._export_comptable)
            self.btn_export_comptable.pack(side="right", padx=(0, 8))

        ctk.CTkLabel(self,
                     text=t("Lecture seule. Onze mesures de santé de "
                            "l'entrepôt central et du réseau régional."),
                     text_color=GRIS, font=ctk.CTkFont(size=11)).pack(
            anchor="w", padx=20, pady=(0, 8))

        # Deux rangées de boutons plutôt qu'une : toutes les sections alignées
        # font plus de 1 300 pixels de large, la fenêtre en fait 1 100 sidebar
        # comprise — la moitié des sections sortait de l'écran, donc était
        # inatteignable. Coupure au milieu et non à un rang fixe : chaque
        # section ajoutée se répartit toute seule, sans faire déborder la
        # première rangée.
        # Les deux rangées partagent la MÊME variable : sélectionner dans l'une
        # désélectionne automatiquement l'autre (CTkSegmentedButton désélectionne
        # tout quand la valeur reçue ne fait pas partie de sa liste).
        self.section_var = tk.StringVar(value=t(self._sections_visibles[0][1]))
        milieu = (len(self._sections_visibles) + 1) // 2
        for rangee in (self._sections_visibles[:milieu],
                       self._sections_visibles[milieu:]):
            if not rangee:
                continue
            choix = ctk.CTkFrame(self, fg_color="transparent")
            choix.pack(fill="x", padx=20, pady=(0, 6))
            ctk.CTkSegmentedButton(
                choix, values=[t(libelle) for _c, libelle in rangee],
                variable=self.section_var,
                command=self._afficher_section).pack(side="left")

        self._conteneur = ctk.CTkFrame(self, fg_color="transparent")
        self._conteneur.pack(fill="both", expand=True)

        for code, _libelle in self._sections_visibles:
            cadre = ctk.CTkFrame(self._conteneur, fg_color="transparent")
            self._sections[code] = cadre
            getattr(self, f"_build_{code}")(cadre)

    def _afficher_section(self, libelle_choisi):
        code = self._libelles.get(libelle_choisi, self._sections_visibles[0][0])
        for cadre in self._sections.values():
            cadre.pack_forget()
        self._sections[code].pack(fill="both", expand=True)

    def _table(self, parent, colonnes, entetes, largeurs, alignements=()):
        cadre = ctk.CTkFrame(parent, fg_color=FOND, corner_radius=10,
                             border_width=1, border_color=BORDURE)
        cadre.pack(fill="both", expand=True, padx=20, pady=(0, 12))
        tree = ttk.Treeview(cadre, columns=colonnes, show="headings")
        for col in colonnes:
            tree.heading(col, text=entetes[col],
                         command=lambda c=col: _sort_treeview(tree, c))
            tree.column(col, width=largeurs[col],
                        anchor="e" if col in alignements else "w")
        tree.pack(side="left", fill="both", expand=True)
        barre = ttk.Scrollbar(cadre, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=barre.set)
        barre.pack(side="right", fill="y")
        tree.tag_configure("ok", foreground=VERT)
        tree.tag_configure("attention", foreground=ORANGE)
        tree.tag_configure("grave", foreground=ROUGE)
        return tree

    def _bandeau(self, parent):
        """Ligne de chiffres clés au-dessus d'un tableau."""
        cadre = ctk.CTkFrame(parent, fg_color=FOND, corner_radius=10,
                             border_width=1, border_color=BORDURE)
        cadre.pack(fill="x", padx=20, pady=(0, 8))
        return cadre

    def _chiffre(self, parent, titre):
        bloc = ctk.CTkFrame(parent, fg_color="transparent")
        bloc.pack(side="left", padx=18, pady=12)
        ctk.CTkLabel(bloc, text=t(titre), text_color=GRIS,
                     font=ctk.CTkFont(size=10)).pack(anchor="w")
        valeur = ctk.CTkLabel(bloc, text="—", text_color="#f0f0f0",
                              font=ctk.CTkFont(size=18, weight="bold"))
        valeur.pack(anchor="w")
        return valeur

    def _statut(self, parent):
        lbl = ctk.CTkLabel(parent, text="", font=ctk.CTkFont(size=11),
                           text_color=GRIS, wraplength=900, justify="left")
        lbl.pack(anchor="w", padx=20, pady=(0, 8))
        return lbl

    # ------------------------------------------- 1. précision d'inventaire

    def _build_precision(self, parent):
        bandeau = self._bandeau(parent)
        self._p_moyenne = self._chiffre(bandeau, "Précision moyenne")
        self._p_ponderee = self._chiffre(bandeau, "Précision pondérée")
        self._p_produits = self._chiffre(bandeau, "Produits comptés")
        self._p_conformes = self._chiffre(bandeau, "Comptages conformes")
        self._p_ecart = self._chiffre(bandeau, "Écart absolu cumulé")
        self._p_statut = self._statut(parent)

        colonnes = ("sku", "name", "theorique", "compte", "ecart", "precision", "date")
        self._t_precision = self._table(
            parent, colonnes,
            {"sku": "SKU", "name": t("Désignation"), "theorique": t("Théorique"),
             "compte": t("Compté"), "ecart": t("Écart"),
             "precision": t("Précision (%)"), "date": t("Comptage")},
            {"sku": 130, "name": 250, "theorique": 100, "compte": 100,
             "ecart": 100, "precision": 100, "date": 150},
            alignements=("theorique", "compte", "ecart", "precision"))

    def _maj_precision(self, data: dict):
        moyenne = data.get("precision_moyenne")
        self._p_moyenne.configure(text=_pct(moyenne),
                                  text_color=_couleur_precision(moyenne))
        self._p_ponderee.configure(text=_pct(data.get("precision_ponderee")))
        self._p_produits.configure(text=_fmt(data.get("produits_comptes")))
        seuil = data.get("seuil_conformite") or 95
        self._p_conformes.configure(
            text=f"{_fmt(data.get('produits_conformes'))} "
                 f"(≥ {_fmt(seuil)} %)")
        self._p_ecart.configure(text=_fmt(data.get("ecart_absolu_total")))

        self._t_precision.delete(*self._t_precision.get_children())
        lignes = data.get("lignes") or []
        for i, l in enumerate(lignes):
            precision = l.get("precision")
            tag = ("ok" if precision is not None and precision >= 95
                   else "attention" if precision is not None and precision >= 90
                   else "grave")
            self._t_precision.insert("", "end", iid=str(i), tags=(tag,), values=(
                l.get("sku") or "", l.get("name") or "",
                _fmt(l.get("theorique")), _fmt(l.get("compte")),
                f"{l.get('ecart') or 0:+g}", _pct(precision),
                (l.get("created_at") or "")[:16],
            ))

        if not data.get("dernier_comptage"):
            self._p_statut.configure(
                text=t("Aucun comptage d'inventaire enregistré : la précision "
                       "ne peut pas encore être mesurée."), text_color=GRIS)
            return
        self._p_statut.configure(
            text=t("Cycle du {depuis} au {dernier} ({jours} jour(s)). "
                   "Précision d'une ligne = 100 × (1 − |écart| / |théorique|), "
                   "le théorique étant reconstitué comme compté − écart.").format(
                       depuis=(data.get("depuis") or "")[:10],
                       dernier=(data.get("dernier_comptage") or "")[:10],
                       jours=data.get("jours_cycle")),
            text_color=GRIS)

    # ------------------------------------------ 2. concentration régions

    def _build_regions(self, parent):
        bandeau = self._bandeau(parent)
        self._r_total = self._chiffre(bandeau, "Volume livré (90 j)")
        self._r_nb = self._chiffre(bandeau, "Régions servies")
        self._r_80 = self._chiffre(bandeau, "Régions faisant 80 %")
        self._r_statut = self._statut(parent)

        colonnes = ("rang", "region", "quantite", "nb_bons", "part", "cumul")
        self._t_regions = self._table(
            parent, colonnes,
            {"rang": t("Rang"), "region": t("Région"), "quantite": t("Quantité"),
             "nb_bons": t("Bons"), "part": t("Part"), "cumul": t("Cumul")},
            {"rang": 60, "region": 200, "quantite": 130, "nb_bons": 90,
             "part": 100, "cumul": 100},
            alignements=("rang", "quantite", "nb_bons", "part", "cumul"))

    def _maj_regions(self, data: dict):
        self._r_total.configure(text=_fmt(data.get("total")))
        self._r_nb.configure(text=_fmt(data.get("nb_regions")))
        self._r_80.configure(text=_fmt(data.get("regions_pour_80")))

        self._t_regions.delete(*self._t_regions.get_children())
        lignes = data.get("lignes") or []
        seuil = data.get("regions_pour_80") or 0
        for i, l in enumerate(lignes):
            # Les régions du « noyau » (celles qui composent les 80 %) sont
            # mises en avant : ce sont elles qu'un arbitrage de stock impacte.
            tag = "attention" if i < seuil else ""
            self._t_regions.insert("", "end", iid=str(i), tags=(tag,), values=(
                i + 1, l.get("region") or "", _fmt(l.get("quantite")),
                _fmt(l.get("nb_bons")), _pct(l.get("part")),
                _pct(l.get("part_cumulee")),
            ))

        if not lignes:
            self._r_statut.configure(
                text=t("Aucune expédition sur la période."), text_color=GRIS)
            return
        self._r_statut.configure(
            text=t("Sur {jours} jours, {n} région(s) concentrent 80 % du "
                   "volume livré (lignes en orange).").format(
                       jours=data.get("jours"), n=seuil),
            text_color=GRIS)

    # -------------------------------------------- 3. comptages à temps

    def _build_comptages(self, parent):
        bandeau = self._bandeau(parent)
        self._c_taux = self._chiffre(bandeau, "Taux du mois")
        self._c_rendus = self._chiffre(bandeau, "Comptages rendus")
        self._c_manquants = self._chiffre(bandeau, "Régions en retard")
        self._c_statut = self._statut(parent)

        colonnes = ("region", "etat", "nb_lignes", "nb_jours", "dernier")
        self._t_comptages = self._table(
            parent, colonnes,
            {"region": t("Région"), "etat": t("État"),
             "nb_lignes": t("Lignes comptées"), "nb_jours": t("Jours de comptage"),
             "dernier": t("Dernier comptage")},
            {"region": 200, "etat": 150, "nb_lignes": 140, "nb_jours": 150,
             "dernier": 170},
            alignements=("nb_lignes", "nb_jours"))

    def _maj_comptages(self, data: dict):
        taux = data.get("taux")
        self._c_taux.configure(
            text=_pct(taux),
            text_color=VERT if taux == 100 else ORANGE if (taux or 0) >= 50 else ROUGE)
        self._c_rendus.configure(
            text=f"{data.get('regions_a_temps', 0)} / {data.get('regions_attendues', 0)}")
        manquantes = data.get("manquantes") or []
        self._c_manquants.configure(text=_fmt(len(manquantes)))

        self._t_comptages.delete(*self._t_comptages.get_children())
        i = 0
        for l in data.get("a_temps") or []:
            self._t_comptages.insert("", "end", iid=str(i), tags=("ok",), values=(
                l.get("region") or "", t("Rendu"), _fmt(l.get("nb_lignes")),
                _fmt(l.get("nb_jours")), (l.get("dernier") or "")[:16],
            ))
            i += 1
        for region in manquantes:
            self._t_comptages.insert("", "end", iid=str(i), tags=("grave",), values=(
                region, t("Non rendu"), "—", "—", "—"))
            i += 1

        hors = data.get("hors_perimetre") or []
        message = t("Mois de {mois} : {rendus} région(s) sur {total} à entrepôt "
                    "régional ont rendu un comptage.").format(
                        mois=data.get("mois") or "", rendus=data.get("regions_a_temps"),
                        total=data.get("regions_attendues"))
        if manquantes:
            message += t(" En retard : {liste}.").format(liste=", ".join(manquantes))
        if hors:
            message += t(" Comptages hors référentiel : {liste}.").format(
                liste=", ".join(hors))
        self._c_statut.configure(
            text=message, text_color=GRIS if not manquantes else ORANGE)

    # ---------------------------------------------- 4. anomalies du mois

    def _build_anomalies(self, parent):
        bandeau = self._bandeau(parent)
        self._a_nb = self._chiffre(bandeau, "Anomalies détectées")
        self._a_max = self._chiffre(bandeau, "Écart maximal")
        self._a_statut = self._statut(parent)

        colonnes = ("date", "type", "sku", "name", "quantite", "moyenne",
                    "ratio", "region", "auteur")
        self._t_anomalies = self._table(
            parent, colonnes,
            {"date": t("Date"), "type": t("Type"), "sku": "SKU",
             "name": t("Désignation"), "quantite": t("Quantité"),
             "moyenne": t("Moyenne habituelle"), "ratio": t("Écart"),
             "region": t("Région"), "auteur": t("Saisi par")},
            {"date": 120, "type": 100, "sku": 120, "name": 200,
             "quantite": 100, "moyenne": 140, "ratio": 90, "region": 140,
             "auteur": 140},
            alignements=("quantite", "moyenne", "ratio"))

    def _maj_anomalies(self, data: dict):
        lignes = data.get("lignes") or []
        self._a_nb.configure(
            text=_fmt(data.get("nb_anomalies")),
            text_color=VERT if not lignes else ORANGE)
        self._a_max.configure(
            text=f"×{_fmt(lignes[0].get('ratio'))}" if lignes else "—")

        self._t_anomalies.delete(*self._t_anomalies.get_children())
        libelles_type = {"RECEIVING": t("Réception"), "DELIVERY": t("Expédition")}
        for i, l in enumerate(lignes):
            ratio = l.get("ratio") or 0
            tag = "grave" if ratio >= 5 else "attention"
            self._t_anomalies.insert("", "end", iid=str(i), tags=(tag,), values=(
                (l.get("created_at") or "")[:16],
                libelles_type.get(l.get("type"), l.get("type") or ""),
                l.get("sku") or "", l.get("name") or "",
                _fmt(l.get("quantite")), _fmt(l.get("moyenne_historique")),
                f"×{_fmt(ratio)}", l.get("region") or "—",
                l.get("created_by") or "—",
            ))

        if not lignes:
            self._a_statut.configure(
                text=t("Aucun mouvement inhabituel ce mois-ci : toutes les "
                       "quantités restent proches des habitudes de leur produit."),
                text_color=VERT)
            return
        self._a_statut.configure(
            text=t("Mouvements du mois dont la quantité dépasse {facteur}× la "
                   "moyenne du même produit sur les mois précédents "
                   "({mini} mouvements historiques minimum). Un écart n'est pas "
                   "une faute : il se vérifie.").format(
                       facteur=_fmt(data.get("facteur")),
                       mini=data.get("min_historique")),
            text_color=GRIS_CLAIR)

    # ------------------------------------------ 5. consommation régionale

    def _build_consommation(self, parent):
        bandeau = self._bandeau(parent)
        self._k_total = self._chiffre(bandeau, "Volume sorti (12 mois)")
        self._k_regions = self._chiffre(bandeau, "Régions servies")
        self._k_tete = self._chiffre(bandeau, "Région en tête")
        self._k_statut = self._statut(parent)

        # Un tableau plat région / produit / mois, triable par n'importe
        # quelle colonne (en-têtes cliquables, comme les autres sections) :
        # aucune bibliothèque de graphiques n'est embarquée dans le client, et
        # aucun autre rapport n'en a jamais eu besoin. Trier par « Mois » puis
        # lire une colonne donne exactement la même lecture qu'une courbe.
        colonnes = ("region", "sku", "name", "mois", "quantite", "unite")
        self._t_consommation = self._table(
            parent, colonnes,
            {"region": t("Région"), "sku": "SKU", "name": t("Désignation"),
             "mois": t("Mois"), "quantite": t("Quantité sortie"),
             "unite": t("Unité")},
            {"region": 150, "sku": 130, "name": 250, "mois": 100,
             "quantite": 140, "unite": 90},
            alignements=("quantite",))

    def _maj_consommation(self, data: dict):
        lignes = data.get("lignes") or []
        totaux = data.get("totaux_par_region") or []
        self._k_total.configure(text=_fmt(data.get("total")))
        self._k_regions.configure(text=_fmt(len(totaux)))
        self._k_tete.configure(
            text=(totaux[0].get("region") or "—") if totaux else "—")

        self._t_consommation.delete(*self._t_consommation.get_children())
        for i, l in enumerate(lignes):
            self._t_consommation.insert("", "end", iid=str(i), values=(
                l.get("region") or "", l.get("sku") or "", l.get("name") or "",
                l.get("mois") or "", _fmt(l.get("quantite")),
                l.get("unit") or "",
            ))

        if not lignes:
            self._k_statut.configure(
                text=t("Aucune sortie enregistrée sur la période."),
                text_color=GRIS)
            return
        tete = ", ".join(
            t("{region} ({q})").format(region=r.get("region"),
                                       q=_fmt(r.get("quantite")))
            for r in totaux[:3])
        self._k_statut.configure(
            text=t("Sorties sur {mois} mois, par région et par produit. "
                   "Une sortie est une marchandise qui quitte un entrepôt "
                   "vers une région : expédition du central, ou transfert "
                   "d'une région vers une autre (comptée pour la région qui "
                   "s'en dessaisit). En tête : {tete}.").format(
                       mois=data.get("mois"), tete=tete),
            text_color=GRIS_CLAIR)

    # ---------------------------------------------- 6. produits dormants

    def _build_dormants(self, parent):
        bandeau = self._bandeau(parent)
        self._d_nb = self._chiffre(bandeau, "Produits sans sortie")
        self._d_avec_stock = self._chiffre(bandeau, "Dont encore en stock")
        self._d_valeur = self._chiffre(bandeau, "Valeur immobilisée")
        self._d_jamais = self._chiffre(bandeau, "Jamais sortis")
        self._d_statut = self._statut(parent)

        colonnes = ("sku", "name", "category", "stock", "cout", "valeur",
                    "jours", "derniere")
        self._t_dormants = self._table(
            parent, colonnes,
            {"sku": "SKU", "name": t("Désignation"), "category": t("Catégorie"),
             "stock": t("Stock"), "cout": t("Coût unitaire"),
             "valeur": t("Valeur"), "jours": t("Jours sans sortie"),
             "derniere": t("Dernière sortie")},
            {"sku": 120, "name": 230, "category": 130, "stock": 90,
             "cout": 110, "valeur": 120, "jours": 130, "derniere": 140},
            alignements=("stock", "cout", "valeur", "jours"))

    def _maj_dormants(self, data: dict):
        self._d_nb.configure(text=_fmt(data.get("nb_produits")))
        self._d_avec_stock.configure(text=_fmt(data.get("nb_avec_stock")))
        self._d_valeur.configure(text=_fmt(data.get("valeur_immobilisee")))
        self._d_jamais.configure(text=_fmt(data.get("nb_jamais_sorti")))

        self._t_dormants.delete(*self._t_dormants.get_children())
        lignes = data.get("lignes") or []
        for i, l in enumerate(lignes):
            # L'alerte porte sur l'ARGENT qui dort, pas sur la ligne de
            # catalogue : un produit dormant à stock nul ne coûte rien.
            stock = l.get("current_stock") or 0
            valeur = l.get("valeur") or 0
            tag = "grave" if valeur > 0 else "attention" if stock > 0 else ""
            self._t_dormants.insert("", "end", iid=str(i), tags=(tag,), values=(
                l.get("sku") or "", l.get("name") or "",
                l.get("category") or "—", _fmt(stock),
                _fmt(l.get("unit_cost")) if l.get("cout_renseigne") else t("non renseigné"),
                _fmt(valeur),
                _fmt(l.get("jours_sans_sortie")) if l.get("jours_sans_sortie") is not None
                else t("jamais"),
                (l.get("derniere_sortie") or "")[:10] or "—",
            ))

        if not lignes:
            self._d_statut.configure(
                text=t("Aucun produit dormant : tout le catalogue actif a "
                       "connu au moins une sortie sur la période."),
                text_color=VERT)
            return
        message = t("Produits actifs sans aucune sortie (expédition, transfert "
                    "régional, ou ajustement à la baisse) depuis {jours} jours. "
                    "La valeur est stock × coût unitaire.").format(
                        jours=data.get("jours"))
        sans_cout = data.get("nb_sans_cout") or 0
        if sans_cout:
            message += t(" {n} produit(s) en stock n'ont pas de coût unitaire : "
                         "la valeur immobilisée est donc sous-estimée.").format(
                             n=sans_cout)
        self._d_statut.configure(text=message,
                                 text_color=ORANGE if sans_cout else GRIS_CLAIR)

    # -------------------------------------- 7. résolution des alertes

    def _build_alertes(self, parent):
        bandeau = self._bandeau(parent)
        self._al_taux = self._chiffre(bandeau, "Taux de résolution")
        self._al_ouverts = self._chiffre(bandeau, "Signalements ouverts")
        self._al_delai = self._chiffre(bandeau, "Délai moyen")
        self._al_statut = self._statut(parent)

        colonnes = ("region", "total", "resolus", "ouverts", "taux",
                    "delai", "delai_max", "attente")
        self._t_alertes = self._table(
            parent, colonnes,
            {"region": t("Région"), "total": t("Signalements"),
             "resolus": t("Résolus"), "ouverts": t("Ouverts"),
             "taux": t("Taux"), "delai": t("Délai moyen (h)"),
             "delai_max": t("Délai max (h)"),
             "attente": t("Plus ancien ouvert (j)")},
            {"region": 170, "total": 110, "resolus": 100, "ouverts": 100,
             "taux": 100, "delai": 130, "delai_max": 120, "attente": 170},
            alignements=("total", "resolus", "ouverts", "taux", "delai",
                         "delai_max", "attente"))

    def _maj_alertes(self, data: dict):
        taux = data.get("taux_resolution")
        self._al_taux.configure(text=_pct(taux),
                                text_color=_couleur_precision(taux))
        ouverts = data.get("ouverts") or 0
        self._al_ouverts.configure(text=_fmt(ouverts),
                                   text_color=VERT if not ouverts else ORANGE)
        delai = data.get("delai_moyen_h")
        self._al_delai.configure(
            text="—" if delai is None else t("{h} h").format(h=_fmt(delai)))

        self._t_alertes.delete(*self._t_alertes.get_children())
        for i, l in enumerate(data.get("lignes") or []):
            tag = ("grave" if l.get("ouverts")
                   else "ok" if l.get("taux_resolution") == 100 else "attention")
            attente = l.get("jours_plus_ancien_ouvert")
            self._t_alertes.insert("", "end", iid=str(i), tags=(tag,), values=(
                l.get("region") or "", _fmt(l.get("total")),
                _fmt(l.get("resolus")), _fmt(l.get("ouverts")),
                _pct(l.get("taux_resolution")), _fmt(l.get("delai_moyen_h")),
                _fmt(l.get("delai_max_h")),
                "—" if attente is None else _fmt(attente),
            ))

        if not data.get("total"):
            self._al_statut.configure(
                text=t("Aucun signalement de rupture régionale sur la période."),
                text_color=GRIS)
            return
        self._al_statut.configure(
            text=t("Signalements ouverts par les régions sur {jours} jours. Le "
                   "délai court de l'ouverture par la région à la résolution "
                   "par le central. Une région à fort taux mais dont le plus "
                   "ancien signalement traîne n'est pas bien servie.").format(
                       jours=data.get("jours")),
            text_color=GRIS_CLAIR)

    # ------------------------------------------- 8. dette de consigne

    def _build_consigne(self, parent):
        bandeau = self._bandeau(parent)
        self._cg_detenteurs = self._chiffre(bandeau, "Détenteurs en dette")
        self._cg_bouteilles = self._chiffre(bandeau, "Bouteilles dues")
        self._cg_ancien = self._chiffre(bandeau, "Dette la plus ancienne")
        self._cg_statut = self._statut(parent)

        colonnes = ("region", "technicien", "solde", "mouvements",
                    "dernier", "jours")
        self._t_consigne = self._table(
            parent, colonnes,
            {"region": t("Région"), "technicien": t("Technicien"),
             "solde": t("Bouteilles dues"), "mouvements": t("Mouvements"),
             "dernier": t("Dernier mouvement"), "jours": t("Ancienneté (j)")},
            {"region": 180, "technicien": 200, "solde": 140,
             "mouvements": 120, "dernier": 160, "jours": 130},
            alignements=("solde", "mouvements", "jours"))

    def _maj_consigne(self, data: dict):
        self._cg_detenteurs.configure(text=_fmt(data.get("nb_detenteurs")))
        dues = data.get("bouteilles_dues") or 0
        self._cg_bouteilles.configure(text=_fmt(dues),
                                      text_color=VERT if not dues else ORANGE)
        ancien = data.get("plus_ancienne_dette_jours")
        self._cg_ancien.configure(
            text="—" if ancien is None else t("{j} j").format(j=_fmt(ancien)))

        self._t_consigne.delete(*self._t_consigne.get_children())
        for i, l in enumerate(data.get("lignes") or []):
            solde = l.get("solde") or 0
            jours = l.get("jours_depuis_dernier")
            # Un solde négatif = plus de retours que de sorties : une erreur de
            # saisie, pas une dette. Signalée en rouge pour être corrigée.
            if solde < 0:
                tag = "grave"
            elif jours is not None and jours >= 60:
                tag = "grave"
            else:
                tag = "attention"
            self._t_consigne.insert("", "end", iid=str(i), tags=(tag,), values=(
                l.get("region") or "", l.get("technician") or t("(région seule)"),
                _fmt(solde), _fmt(l.get("nb_mouvements")),
                (l.get("dernier_mouvement") or "")[:16] or "—",
                "—" if jours is None else _fmt(jours),
            ))

        negatifs = data.get("nb_soldes_negatifs") or 0
        if not (data.get("lignes") or []):
            self._cg_statut.configure(
                text=t("Aucune bouteille consignée en attente de retour."),
                text_color=VERT)
            return
        message = t("Solde net par détenteur : bouteilles parties moins "
                    "bouteilles rendues. Une livraison nominative apparaît sur "
                    "la ligne du technicien, jamais en double avec sa région.")
        if negatifs:
            message += t(" {n} solde(s) négatif(s) : plus de retours que de "
                         "sorties, à corriger.").format(n=negatifs)
        self._cg_statut.configure(text=message,
                                  text_color=ORANGE if negatifs else GRIS_CLAIR)

    # ---------------------------------------- 9. activité par opérateur

    def _build_operateurs(self, parent):
        bandeau = self._bandeau(parent)
        self._op_nb = self._chiffre(bandeau, "Opérateurs actifs")
        self._op_bons = self._chiffre(bandeau, "Bons saisis")
        self._op_hors = self._chiffre(bandeau, "Saisies hors horaires")
        self._op_statut = self._statut(parent)

        colonnes = ("operateur", "bons", "part", "annules", "hors_heures",
                    "pic", "dernier")
        self._t_operateurs = self._table(
            parent, colonnes,
            {"operateur": t("Compte"), "bons": t("Bons saisis"),
             "part": t("Part"), "annules": t("Annulés"),
             "hors_heures": t("Hors horaires"), "pic": t("Heure de pointe"),
             "dernier": t("Dernière saisie")},
            {"operateur": 200, "bons": 110, "part": 90, "annules": 100,
             "hors_heures": 140, "pic": 130, "dernier": 160},
            alignements=("bons", "part", "annules", "hors_heures"))

    def _maj_operateurs(self, data: dict):
        lignes = data.get("lignes") or []
        total = data.get("total_bons") or 0
        hors = data.get("total_hors_heures") or 0
        self._op_nb.configure(text=_fmt(data.get("nb_operateurs")))
        self._op_bons.configure(text=_fmt(total))
        self._op_hors.configure(text=_fmt(hors),
                                text_color=VERT if not hors else ORANGE)

        self._t_operateurs.delete(*self._t_operateurs.get_children())
        for i, l in enumerate(lignes):
            part_hors = l.get("part_hors_heures") or 0
            tag = "grave" if part_hors >= 25 else "attention" if part_hors else "ok"
            pic = l.get("heure_pic")
            self._t_operateurs.insert("", "end", iid=str(i), tags=(tag,), values=(
                l.get("operateur") or "", _fmt(l.get("nb_bons")),
                _pct(100.0 * (l.get("nb_bons") or 0) / total) if total else "—",
                _fmt(l.get("annules")),
                t("{n} ({p})").format(n=_fmt(l.get("hors_heures")),
                                      p=_pct(part_hors)),
                "—" if pic is None else t("{h} h").format(h=pic),
                (l.get("dernier") or "")[:16] or "—",
            ))

        if not lignes:
            self._op_statut.configure(
                text=t("Aucun bon saisi sur la période."), text_color=GRIS)
            return
        self._op_statut.configure(
            text=t("Bons créés sur {jours} jours, par compte de saisie "
                   "(annulés compris : le bon a quand même été tapé). "
                   "« Hors horaires » = saisie avant {debut} h ou après "
                   "{fin} h. Ce n'est pas une faute : c'est une question à "
                   "poser.").format(
                       jours=data.get("jours"),
                       debut=data.get("heure_ouvrable_debut"),
                       fin=data.get("heure_ouvrable_fin")),
            text_color=GRIS_CLAIR)

    # ------------------------------------------ 10. écarts de réception

    def _build_ecarts(self, parent):
        bandeau = self._bandeau(parent)
        self._e_manquants = self._chiffre(bandeau, "Manquants (jamais arrivés)")
        self._e_excedents = self._chiffre(bandeau, "Excédents (reçus en trop)")
        self._e_taux = self._chiffre(bandeau, "Taux de manquant")
        self._e_bons = self._chiffre(bandeau, "Réceptions en écart")
        self._e_statut = self._statut(parent)

        # Manquants et excédents restent dans DEUX colonnes distinctes, comme
        # le serveur les renvoie : un net nul peut cacher un manquant et un
        # excédent qui n'ont rien à voir l'un avec l'autre.
        colonnes = ("region", "transporteur", "bons", "expedie", "recu",
                    "manquants", "excedents", "net", "taux")
        self._t_ecarts = self._table(
            parent, colonnes,
            {"region": t("Région destinataire"), "transporteur": t("Transporteur"),
             "bons": t("Bons"), "expedie": t("Expédié"), "recu": t("Reçu"),
             "manquants": t("Manquants"), "excedents": t("Excédents"),
             "net": t("Net"), "taux": t("Taux manquant")},
            {"region": 170, "transporteur": 160, "bons": 70, "expedie": 110,
             "recu": 110, "manquants": 110, "excedents": 110, "net": 100,
             "taux": 120},
            alignements=("bons", "expedie", "recu", "manquants", "excedents",
                         "net", "taux"))

    def _maj_ecarts(self, data: dict):
        manquants = data.get("manquants_total") or 0
        excedents = data.get("excedents_total") or 0
        self._e_manquants.configure(text=_fmt(manquants),
                                    text_color=VERT if not manquants else ROUGE)
        self._e_excedents.configure(text=_fmt(excedents),
                                    text_color=VERT if not excedents else ORANGE)
        taux = data.get("taux_manquant") or 0
        self._e_taux.configure(
            text=_pct(taux),
            text_color=VERT if taux < 0.5 else ORANGE if taux < 2 else ROUGE)
        self._e_bons.configure(
            text=f"{_fmt(data.get('nb_bons_en_ecart'))} / "
                 f"{_fmt(data.get('nb_receptions'))}")

        self._t_ecarts.delete(*self._t_ecarts.get_children())
        for i, l in enumerate(data.get("lignes") or []):
            ligne_manquants = l.get("manquants") or 0
            ligne_excedents = l.get("excedents") or 0
            if ligne_manquants:
                tag = "grave"
            elif ligne_excedents:
                tag = "attention"
            else:
                tag = "ok"
            self._t_ecarts.insert("", "end", iid=str(i), tags=(tag,), values=(
                l.get("region") or "—",
                l.get("carrier") or t("(non renseigné)"),
                _fmt(l.get("nb_bons")), _fmt(l.get("expedie")),
                _fmt(l.get("recu")), _fmt(ligne_manquants),
                _fmt(ligne_excedents), f"{l.get('net') or 0:+g}",
                _pct(l.get("taux_manquant")),
            ))

        if not (data.get("lignes") or []):
            self._e_statut.configure(
                text=t("Aucune réception confirmée sur la période : rien à "
                       "comparer."),
                text_color=GRIS)
            return
        message = t("Écart entre la quantité expédiée et la quantité confirmée "
                    "reçue, cumulé sur {jours} jours par région destinataire "
                    "et par transporteur. Manquants et excédents sont comptés "
                    "séparément à dessein : les additionner effacerait deux "
                    "anomalies l'une par l'autre.").format(
                        jours=data.get("jours"))
        if data.get("detail_tronque"):
            message += t(" (Le compte de bons en écart est plafonné : "
                         "resserre la période pour le chiffre exact.)")
        self._e_statut.configure(
            text=message, text_color=ORANGE if manquants else GRIS_CLAIR)

    # ------------------------------------- 11. régularité des fournisseurs

    def _build_fournisseurs(self, parent):
        bandeau = self._bandeau(parent)
        self._f_fournisseurs = self._chiffre(bandeau, "Fournisseurs mesurés")
        self._f_intervalle = self._chiffre(bandeau, "Rythme moyen (jours)")
        self._f_reguliers = self._chiffre(bandeau, "Rythme régulier")
        self._f_retard = self._chiffre(bandeau, "Au-delà de leur rythme")
        self._f_statut = self._statut(parent)

        # « Rythme » et non « délai » dans TOUS les libellés : le serveur ne
        # mesure pas un délai commande -> livraison (aucune date de commande
        # n'existe en base). Un en-tête « Délai » aurait fait lire un chiffre
        # pour ce qu'il n'est pas.
        colonnes = ("nom", "receptions", "rythme", "min", "max", "regulier",
                    "derniere", "depuis", "retard", "annonce", "devise",
                    "conditions")
        self._t_fournisseurs = self._table(
            parent, colonnes,
            {"nom": t("Fournisseur"), "receptions": t("Réceptions"),
             "rythme": t("Rythme moyen (j)"), "min": t("Plus court (j)"),
             "max": t("Plus long (j)"), "regulier": t("Régulier"),
             "derniere": t("Dernière"), "depuis": t("Depuis (j)"),
             "retard": t("Écart au rythme (j)"), "annonce": t("Annoncé (j)"),
             "devise": t("Devise"), "conditions": t("Paiement")},
            {"nom": 190, "receptions": 90, "rythme": 120, "min": 100,
             "max": 100, "regulier": 80, "derniere": 100, "depuis": 90,
             "retard": 130, "annonce": 90, "devise": 70, "conditions": 130},
            alignements=("receptions", "rythme", "min", "max", "depuis",
                         "retard", "annonce"))

    def _maj_fournisseurs(self, data: dict):
        self._f_fournisseurs.configure(text=_fmt(data.get("nb_fournisseurs")))
        self._f_intervalle.configure(
            text=_fmt(data.get("intervalle_moyen_global")))
        mesures = data.get("nb_fournisseurs") or 0
        reguliers = data.get("nb_reguliers") or 0
        self._f_reguliers.configure(
            text=f"{_fmt(reguliers)} / {_fmt(mesures)}",
            text_color=VERT if mesures and reguliers == mesures else GRIS_CLAIR)
        en_retard = data.get("nb_en_retard") or 0
        self._f_retard.configure(
            text=_fmt(en_retard),
            text_color=VERT if not en_retard else ORANGE)

        self._t_fournisseurs.delete(*self._t_fournisseurs.get_children())
        lignes = data.get("lignes") or []
        for i, l in enumerate(lignes):
            retard = l.get("retard")
            rythme = l.get("rythme") or l.get("intervalle_moyen") or 0
            # Rouge seulement au-delà du DOUBLE du rythme habituel : dépasser
            # son rythme de deux jours n'est pas un incident, le dépasser d'un
            # cycle entier en est un.
            if retard is not None and rythme and retard >= rythme:
                tag = "grave"
            elif retard is not None and retard > 0:
                tag = "attention"
            else:
                tag = "ok"
            self._t_fournisseurs.insert("", "end", iid=f"m{i}", tags=(tag,), values=(
                l.get("name") or "—",
                _fmt(l.get("nb_receptions")),
                _fmt(l.get("intervalle_moyen")),
                _fmt(l.get("intervalle_min")),
                _fmt(l.get("intervalle_max")),
                t("Oui") if l.get("regulier") else t("Non"),
                l.get("derniere") or "—",
                _fmt(l.get("jours_depuis_derniere")),
                "—" if retard is None else f"{retard:+g}",
                _fmt(l.get("delai_annonce")),
                l.get("devise") or "",
                l.get("conditions_paiement") or "",
            ))

        # Les fournisseurs à réception unique ferment la liste, sans rythme :
        # les cacher laisserait croire qu'ils n'ont rien livré du tout.
        for i, l in enumerate(data.get("sans_historique") or []):
            self._t_fournisseurs.insert("", "end", iid=f"s{i}", values=(
                l.get("name") or "—",
                _fmt(l.get("nb_receptions")),
                "—", "—", "—", "—",
                l.get("derniere") or "—",
                _fmt(l.get("jours_depuis_derniere")),
                "—",
                _fmt(l.get("delai_annonce")),
                l.get("devise") or "",
                l.get("conditions_paiement") or "",
            ))

        if not lignes and not (data.get("sans_historique") or []):
            self._f_statut.configure(
                text=t("Aucune réception rattachée à un fournisseur enregistré "
                       "sur la période. Rattache le fournisseur au bon de "
                       "réception (champ « Fournisseur ») pour alimenter ce "
                       "rapport."),
                text_color=GRIS)
            return

        # Avertissement affiché EN PERMANENCE, pas seulement en cas de
        # problème : le titre de la section pourrait laisser croire à un délai
        # de livraison, et un chiffre mal compris est pire qu'un chiffre absent.
        message = t("Ce rapport mesure le RYTHME d'approvisionnement — le "
                    "nombre de jours entre deux réceptions successives d'un "
                    "même fournisseur — sur {jours} jours. Ce n'est PAS un "
                    "délai entre la commande et la livraison : le système "
                    "n'enregistre aucune date de commande. La colonne "
                    "« Annoncé » est le délai que le fournisseur déclare, "
                    "saisi à la main sur sa fiche contact.").format(
                        jours=data.get("jours"))
        sans_contact = data.get("nb_receptions_sans_contact") or 0
        if sans_contact:
            message += t(" {n} réception(s) de la période ne sont rattachées à "
                         "aucun fournisseur enregistré et ne sont donc pas "
                         "comptées ici.").format(n=sans_contact)
        self._f_statut.configure(
            text=message,
            text_color=ORANGE if data.get("nb_en_retard") else GRIS_CLAIR)

    # -------------------------------------------- actions d'administration

    def _export_comptable(self):
        """Télécharge le classeur comptable, puis demande où l'enregistrer.

        Ordre volontaire : on n'ouvre la boîte « Enregistrer sous » qu'une
        fois les octets en main. Un refus du serveur n'aboutit donc jamais à
        un fichier vide sur le bureau de l'administrateur.
        """
        self.btn_export_comptable.configure(state="disabled",
                                            text=t("Génération..."))

        def _termine(contenu: bytes):
            self.btn_export_comptable.configure(state="normal",
                                                text=t("📊  Export comptable"))
            chemin = filedialog.asksaveasfilename(
                defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")],
                initialfile=f"comptable_{date.today().strftime('%Y-%m-%d')}.xlsx")
            if not chemin:
                return
            try:
                Path(chemin).write_bytes(contenu)
            except OSError as exc:
                messagebox.showerror(
                    t("Export comptable"),
                    t("Impossible d'écrire le fichier :\n{erreur}").format(erreur=exc))
                return
            messagebox.showinfo(t("Export comptable"),
                                t("Fichier enregistré :\n{chemin}").format(chemin=chemin))

        def _echec(exc):
            self.btn_export_comptable.configure(state="normal",
                                                text=t("📊  Export comptable"))
            messagebox.showerror(t("Export comptable"), str(exc))

        run_async(self, self.api.export_comptable, _termine, _echec)

    def _envoyer_rapport_mensuel(self):
        """Demande au serveur d'expédier le rapport de stock par e-mail.

        Le serveur refuse (400) tant qu'aucun destinataire n'est configuré :
        son message explique quoi faire et est affiché tel quel, plutôt que
        d'être remplacé par un « échec » qui n'apprendrait rien.
        """
        if not messagebox.askyesno(
                t("Rapport mensuel"),
                t("Envoyer le rapport de stock par e-mail aux destinataires "
                  "configurés sur le serveur ?")):
            return

        self.btn_rapport_email.configure(state="disabled", text=t("Envoi..."))
        libelle = t("✉️  Envoyer le rapport mensuel maintenant")

        def _termine(reponse: dict):
            self.btn_rapport_email.configure(state="normal", text=libelle)
            destinataires = ", ".join(reponse.get("destinataires") or [])
            messagebox.showinfo(
                t("Rapport mensuel"),
                t("Rapport en cours d'envoi à :\n{destinataires}").format(
                    destinataires=destinataires))

        def _echec(exc):
            self.btn_rapport_email.configure(state="normal", text=libelle)
            messagebox.showerror(t("Rapport mensuel"), str(exc))

        run_async(self, self.api.envoyer_rapport_mensuel, _termine, _echec)

    # ------------------------------------------------------------ données

    def refresh(self):
        """Appels indépendants : une section en erreur n'en bloque aucune.

        Seules les sections CONSTRUITES sont rafraîchies : une section masquée
        pour le secteur (voir `SECTIONS_SANS_OBJET_FUEL`) n'a ni tableau ni
        libellé de statut, et l'interroger quand même lèverait une erreur à
        chaque rafraîchissement — pour un résultat qui de toute façon ne
        s'afficherait nulle part.
        """
        appels_par_code = {
            "precision": (self.api.get_precision_inventaire, self._maj_precision,
                          "_p_statut"),
            "regions": (self.api.get_concentration_regions, self._maj_regions,
                        "_r_statut"),
            "comptages": (self.api.get_comptages_a_temps, self._maj_comptages,
                          "_c_statut"),
            "anomalies": (self.api.get_anomalies_mouvements, self._maj_anomalies,
                          "_a_statut"),
            "consommation": (self.api.get_consommation_regionale,
                             self._maj_consommation, "_k_statut"),
            "dormants": (self.api.get_produits_dormants, self._maj_dormants,
                         "_d_statut"),
            "alertes": (self.api.get_resolution_alertes, self._maj_alertes,
                        "_al_statut"),
            "consigne": (self.api.get_dette_consigne, self._maj_consigne,
                         "_cg_statut"),
            "operateurs": (self.api.get_activite_operateurs, self._maj_operateurs,
                           "_op_statut"),
            "ecarts": (self.api.get_ecarts_reception, self._maj_ecarts,
                       "_e_statut"),
            "fournisseurs": (self.api.get_delai_fournisseurs,
                             self._maj_fournisseurs, "_f_statut"),
        }
        appels = []
        for code, _libelle in self._sections_visibles:
            appel, appliquer, nom_statut = appels_par_code[code]
            statut = getattr(self, nom_statut)
            statut.configure(text=t("Chargement..."), text_color=GRIS)
            appels.append((appel, appliquer, statut))

        for appel, appliquer, statut in appels:
            run_async(self, appel, appliquer,
                      lambda exc, lbl=statut: lbl.configure(
                          text=str(exc), text_color=ORANGE))
