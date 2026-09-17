import os
import time
import tkinter as tk
import webbrowser
from datetime import date, timedelta
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from async_call import run_async
from i18n import t
from views.pdf_utils import PdfAvecPiedDePage, setup_pdf_fonts, safe_text
from views.utils import sort_treeview as _sort_treeview

# Le WebSocket porte le temps réel : ce polling n'est plus qu'un filet de
# sécurité si la socket tombe.
REFRESH_MS = 30000
BACKOFF_MAX_MS = 30000
# Tant que le WebSocket est connecté, ce polling est redondant : l'espacer
# réduit la charge serveur sans rien perdre. Revient à REFRESH_MS dès que la
# connexion tombe (voir _replanifier).
REFRESH_MS_WS_ACTIF = 120000

COULEURS_CATEGORIES = [
    "#3b82f6", "#22c55e", "#f59e0b", "#a855f7", "#ef4444",
    "#14b8a6", "#ec4899", "#84cc16", "#6366f1", "#f97316",
]


def _fmt(valeur: float) -> str:
    """12345.0 -> '12 345' ; 12.5 -> '12,5'"""
    if valeur is None:
        return "0"
    if abs(valeur - round(valeur)) < 1e-9:
        return f"{int(round(valeur)):,}".replace(",", " ")
    return f"{valeur:,.1f}".replace(",", " ").replace(".", ",")


def _teinte(couleur: str, vers: str, facteur: float) -> str:
    """Mélange une couleur hex vers du blanc ("clair") ou du noir ("sombre").

    `facteur` dans [0, 1] : 0 renvoie la couleur telle quelle, 1 renvoie la
    cible pure. Sert à dériver les nuances de la jauge de cuve (reflet,
    surface du liquide, paroi) à partir de la seule couleur de statut, sans
    maintenir une palette séparée qui pourrait diverger d'elle.
    """
    couleur = couleur.lstrip("#")
    r, g, b = int(couleur[0:2], 16), int(couleur[2:4], 16), int(couleur[4:6], 16)
    cible = 255 if vers == "clair" else 0
    r = round(r + (cible - r) * facteur)
    g = round(g + (cible - g) * facteur)
    b = round(b + (cible - b) * facteur)
    return f"#{r:02x}{g:02x}{b:02x}"


def _libelle_cuve(p: dict) -> str:
    """Nom affiché d'une cuve : ajoute le site si le nom du produit ne le
    porte pas déjà. Purement un affichage — ne modifie jamais `products.name`
    en base ; sert de filet quand la convention de nommage documentée
    (docs/SECTEUR_FUEL.md : "Diesel — WH Central") n'a pas été suivie à la
    création du produit. Constaté par Idy509 le 2026-09-09 : la cuve "Diesel"
    ne montrait aucun site sur sa courbe/jauge, alors que les deux cuves
    "Gasoline — Canapé-Vert N" en montraient un — deux poids deux mesures
    dues uniquement au nom saisi, pas au code."""
    nom = p.get("name") or ""
    site = (p.get("site") or "").strip()
    if not site or site.lower() in nom.lower():
        return nom
    return f"{nom} — {site}"


class KpiCard(ctk.CTkFrame):
    def __init__(self, master, titre: str, couleur: str = "#3b82f6",
                 on_click=None):
        super().__init__(master, corner_radius=10, fg_color="#1a1d23",
                         border_width=1, border_color="#2a2d35")
        self.couleur = couleur
        self._on_click = on_click
        ctk.CTkFrame(self, height=3, corner_radius=2, fg_color=couleur).pack(
            fill="x", padx=0, pady=0, anchor="n"
        )
        self.valeur_label = ctk.CTkLabel(self, text="—",
                                         font=ctk.CTkFont(size=28, weight="bold"),
                                         text_color="#f0f0f0", anchor="w")
        self.valeur_label.pack(fill="x", padx=14, pady=(10, 0))
        self.titre_label = ctk.CTkLabel(self, text=titre, text_color="#9ca3af",
                                        font=ctk.CTkFont(size=11), anchor="w")
        self.titre_label.pack(fill="x", padx=14, pady=(0, 2))
        self.trend_label = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=9),
                                        text_color="#6b7280", anchor="w")
        self.trend_label.pack(fill="x", padx=14, pady=(0, 8))
        if self._on_click:
            self.configure(cursor="hand2")
            for widget in (self, self.valeur_label, self.titre_label, self.trend_label):
                widget.bind("<Button-1>", lambda e, cb=self._on_click: cb())
        self.bind("<Configure>", self._on_resize)

    def _on_resize(self, event):
        w = max(event.width - 32, 60)
        self.trend_label.configure(wraplength=w)

    def set(self, valeur: str, couleur: str | None = None):
        self.valeur_label.configure(text=valeur, text_color=couleur or "#f0f0f0")

    def set_trend(self, text: str, positive: bool = True):
        self.trend_label.configure(text=text,
                                   text_color="#22c55e" if positive else "#ef4444")


class BarChart(ctk.CTkFrame):
    """Graphique en barres horizontales, dessiné sur un Canvas (sans dépendance).

    on_click : appelé avec le libellé de la barre cliquée (détail par catégorie).
    """

    HAUTEUR_BARRE = 22
    ECART = 10

    def __init__(self, master, titre: str, hauteur: int = 260, on_click=None):
        super().__init__(master, corner_radius=10, fg_color="#1a1d23",
                         border_width=1, border_color="#2a2d35")
        self.on_click = on_click

        barre_titre = ctk.CTkFrame(self, fg_color="transparent")
        barre_titre.pack(fill="x", padx=14, pady=(12, 4))
        self.titre_label = ctk.CTkLabel(barre_titre, text=titre,
                                        font=ctk.CTkFont(size=13, weight="bold"))
        self.titre_label.pack(side="left")
        self.retour_btn = ctk.CTkButton(barre_titre, text=t("← Retour"), width=80, height=24,
                                         fg_color="gray30", command=self._retour)
        self.indice_label = ctk.CTkLabel(barre_titre, text="", text_color="gray",
                                          font=ctk.CTkFont(size=9))
        if on_click:
            self.indice_label.configure(text=t("cliquer pour détail"))
            self.indice_label.pack(side="right")

        self.canvas = tk.Canvas(self, height=hauteur, highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True, padx=14, pady=(0, 12))
        self.canvas.bind("<Configure>", lambda e: self._dessiner())
        if on_click:
            self.canvas.bind("<Button-1>", self._sur_clic)
            self.canvas.bind("<Motion>", self._sur_survol)

        self._donnees: list[tuple[str, float]] = []
        self._titre_base = titre
        self._survol = -1

    def set_donnees(self, donnees: list[tuple[str, float]]):
        self._donnees = donnees
        self._dessiner()

    def set_mode_detail(self, titre: str | None):
        """titre=None : revient au mode racine."""
        if titre:
            self.titre_label.configure(text=titre)
            self.indice_label.pack_forget()
            self.retour_btn.pack(side="right")
        else:
            self.titre_label.configure(text=self._titre_base)
            self.retour_btn.pack_forget()
            if self.on_click:
                self.indice_label.pack(side="right")

    def _index_a(self, y: int) -> int:
        pas = self.HAUTEUR_BARRE + self.ECART
        i = int((y - 8) // pas) if y >= 8 else -1
        return i if 0 <= i < len(self._donnees) else -1

    def _sur_clic(self, event):
        i = self._index_a(event.y)
        if i >= 0 and self.on_click:
            self.on_click(self._donnees[i][0])

    def _sur_survol(self, event):
        i = self._index_a(event.y)
        if i != self._survol:
            self._survol = i
            self.canvas.configure(cursor="hand2" if i >= 0 else "")
            self._dessiner()

    def _retour(self):
        if self.on_click:
            self.on_click(None)

    def _dessiner(self):
        c = self.canvas
        c.delete("all")
        fond = self._apply_appearance_mode(self.cget("fg_color"))
        c.configure(bg=fond)

        if not self._donnees:
            c.create_text(12, 16, anchor="w", text=t("Aucune donnée"), fill="gray")
            return

        largeur = c.winfo_width() or 600
        maxi = max(v for _, v in self._donnees) or 1
        libelle_px = min(170, int(largeur * 0.30))
        valeur_px = min(90, int(largeur * 0.15))
        piste = max(largeur - libelle_px - valeur_px - 20, 60)
        hauteur_barre = self.HAUTEUR_BARRE
        ecart = self.ECART

        for i, (libelle, valeur) in enumerate(self._donnees):
            y = 8 + i * (hauteur_barre + ecart)
            couleur = COULEURS_CATEGORIES[i % len(COULEURS_CATEGORIES)]
            actif = i == self._survol

            max_chars = max(libelle_px // 8, 10)
            texte = libelle if len(libelle) <= max_chars else libelle[:max_chars - 1] + "…"
            c.create_text(6, y + hauteur_barre / 2, anchor="w", text=texte,
                          fill="gray95" if actif else "gray70",
                          font=("Segoe UI", 10, "bold" if actif else "normal"))

            # Piste de fond puis barre proportionnelle
            c.create_rectangle(libelle_px, y, libelle_px + piste, y + hauteur_barre,
                               fill="#3a3d3e" if actif else "#2a2d2e", outline="")
            longueur = max((valeur / maxi) * piste, 2)
            c.create_rectangle(libelle_px, y, libelle_px + longueur, y + hauteur_barre,
                               fill=couleur, outline="white" if actif else "", width=1)

            c.create_text(libelle_px + piste + 10, y + hauteur_barre / 2, anchor="w",
                          text=_fmt(valeur), fill="gray95" if actif else "gray80",
                          font=("Segoe UI", 10, "bold"))

        besoin = 8 + len(self._donnees) * (hauteur_barre + ecart)
        c.configure(height=max(besoin, 60))


class JaugeCuve(ctk.CTkFrame):
    """Jauge visuelle d'une cuve : silhouette remplie au niveau réel.

    Rouge sous 15 % (recompléter d'urgence), orange sous 40 %, vert au-delà —
    les mêmes seuils que le coup d'œil qu'un magasinier jette à une jauge
    physique avant de décider s'il commande une livraison.
    """

    LARGEUR = 90
    HAUTEUR_CUVE = 130

    def __init__(self, master, nom: str, niveau: float, capacite: float, on_click=None):
        super().__init__(master, fg_color="transparent")
        # Couleur de fond posée en dur : `self.cget("fg_color")` vaut la
        # chaîne littérale "transparent" (ce cadre l'est), que
        # `_apply_appearance_mode` ne sait pas résoudre en couleur Tk — le
        # canvas retombait alors sur le gris clair par défaut de Tk, un halo
        # clair derrière chaque cuve sur un tableau de bord sombre. Reprend
        # la couleur réelle de la carte qui contient cet écran
        # (`_cadre_cuves`, plus haut dans ce fichier).
        self.canvas = tk.Canvas(self, width=self.LARGEUR, height=self.HAUTEUR_CUVE,
                                highlightthickness=0, bd=0, bg="#1a1d23")
        self.canvas.pack()
        self._nom = nom
        self._selectionnee = False
        self._label_valeur = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11, weight="bold"))
        self._label_valeur.pack(pady=(4, 0))
        self._label_nom = ctk.CTkLabel(self, text=nom, text_color="#9ca3af",
                                       font=ctk.CTkFont(size=10), wraplength=self.LARGEUR)
        self._label_nom.pack()
        self._label_autonomie = ctk.CTkLabel(self, text="", text_color="#6b7280",
                                             font=ctk.CTkFont(size=9))
        self._label_autonomie.pack()
        # Clique une cuve : filtre les graphiques du dessous sur elle seule
        # (demandé par Idy509 le 2026-09-09). Posé une fois ici, jamais
        # réappliqué à chaque redessin de la jauge — même défaut que celui
        # déjà corrigé sur `KpiCard._on_resize`, pas la peine de le répéter.
        if on_click:
            self.configure(cursor="hand2")
            for widget in (self, self.canvas, self._label_valeur,
                           self._label_nom, self._label_autonomie):
                widget.bind("<Button-1>", lambda e, cb=on_click: cb())
        self.maj(niveau, capacite)

    def set_selectionnee(self, selectionnee: bool):
        """Cadre visible quand cette cuve est celle affichée en détail."""
        self._selectionnee = selectionnee
        self.configure(fg_color="#1e2530" if selectionnee else "transparent",
                       corner_radius=8)

    def set_autonomie(self, jours: float | None):
        """Affiche l'autonomie restante (calculée côté serveur à partir de
        la consommation des 30 derniers jours). `None` = pas de conso
        récente sur cette cuve : rien d'utile à afficher, une autonomie
        infinie n'aide personne."""
        if jours is None:
            self._label_autonomie.configure(text="")
            return
        if jours < 7:
            couleur = "#ef4444"
        elif jours < 14:
            couleur = "#f59e0b"
        else:
            couleur = "#6b7280"
        self._label_autonomie.configure(text=f"~{jours:.0f} {t('jours')}", text_color=couleur)

    def maj(self, niveau: float, capacite: float):
        """Dessine la cuve en silo cylindrique : fond bombé, paroi métal, un
        reflet vertical et le liquide qui suit la même courbure — plus proche
        d'une cuve réelle que le simple rectangle arrondi d'avant, sans
        changer ni la taille du widget ni la lecture au clic (`on_click`
        reste posé sur le cadre entier, pas sur le contenu du canvas)."""
        c = self.canvas
        c.delete("all")

        capacite = capacite or 1
        pct = max(0.0, min(1.0, niveau / capacite))
        if pct < 0.15:
            couleur = "#ef4444"
        elif pct < 0.40:
            couleur = "#f59e0b"
        else:
            couleur = "#22c55e"

        x0, x1 = 12, self.LARGEUR - 12
        y_haut, y_bas = 14, self.HAUTEUR_CUVE - 22
        rayon_y = 9  # courbure des fonds bombés (haut/bas) du silo

        vide_paroi = "#20242c"
        vide_capot = _teinte(vide_paroi, "clair", 0.12)
        contour = "#3a3d45"

        # Ombre au sol : ancre visuellement le silo plutôt qu'il ne flotte.
        c.create_oval(x0 + 6, y_bas + rayon_y - 2, x1 - 6, y_bas + rayon_y + 7,
                      fill="#000000", outline="", stipple="gray75")

        # Coque vide : fond bombé, paroi, capot bombé — dans cet ordre, pour
        # que le capot recouvre le haut du fond (illusion de cylindre en 2D).
        c.create_oval(x0, y_bas - rayon_y, x1, y_bas + rayon_y,
                      fill=vide_paroi, outline="")
        c.create_rectangle(x0, y_haut, x1, y_bas, fill=vide_paroi, outline="")
        c.create_oval(x0, y_haut - rayon_y, x1, y_haut + rayon_y,
                      fill=vide_capot, outline="")

        # Liquide : même construction fond/corps/surface, coloré et arrêté à
        # la hauteur du niveau — la surface (méniscus) est une ellipse plus
        # plate qu'un fond de cuve, pour lire comme une nappe et non un dôme.
        if pct > 0.003:
            y_niveau = y_bas - (y_bas - y_haut) * pct
            couleur_surface = _teinte(couleur, "clair", 0.35)
            c.create_oval(x0, y_bas - rayon_y, x1, y_bas + rayon_y,
                          fill=couleur, outline="")
            if y_niveau < y_bas:
                c.create_rectangle(x0, y_niveau, x1, y_bas, fill=couleur, outline="")
            rayon_surface = rayon_y * 0.55
            c.create_oval(x0, y_niveau - rayon_surface, x1, y_niveau + rayon_surface,
                          fill=couleur_surface, outline="")

        # Paroi et capot redessinés en contour seul, par-dessus le liquide :
        # les bords du silo restent nets, plutôt que noyés sous le remplissage.
        c.create_line(x0, y_haut, x0, y_bas, fill=contour, width=2)
        c.create_line(x1, y_haut, x1, y_bas, fill=contour, width=2)
        c.create_oval(x0, y_bas - rayon_y, x1, y_bas + rayon_y, outline=contour, width=2)
        c.create_oval(x0, y_haut - rayon_y, x1, y_haut + rayon_y, outline=contour, width=2)

        # Reflet : bande verticale claire côté gauche, comme la lumière sur
        # une tôle cintrée — c'est ce détail qui vend la forme cylindrique.
        c.create_line(x0 + 9, y_haut + 5, x0 + 9, y_bas - 5,
                      fill="#ffffff", width=3, stipple="gray25")

        # Graduations discrètes à 25/50/75 % : un repère rapide sans chiffres
        # qui surchargeraient une jauge large de 90 px.
        for repere in (0.25, 0.50, 0.75):
            y = y_bas - (y_bas - y_haut) * repere
            c.create_line(x1 + 2, y, x1 + 6, y, fill=contour, width=1)

        centre_y = (y_haut + y_bas) / 2
        dans_le_liquide = pct > 0.003 and centre_y >= (y_bas - (y_bas - y_haut) * pct)
        c.create_text((x0 + x1) / 2, centre_y, text=f"{pct * 100:.0f}%",
                      fill="#0f1115" if (dans_le_liquide and pct > 0.55) else "#e5e7eb",
                      font=("Segoe UI", 12, "bold"))

        self._label_valeur.configure(
            text=f"{_fmt(niveau)} / {_fmt(capacite)} gal", text_color=couleur)


class CourbeNiveauCuve(ctk.CTkFrame):
    """Niveau d'une cuve dans le temps — courbe en escalier sur un Canvas.

    La jauge à côté ne dit que « où on en est » ; celle-ci dit « à quelle
    vitesse on y est arrivé ». C'est ce qui permet de voir qu'une cuve se
    vide plus vite que d'habitude, ou de repérer une chute qu'aucune
    livraison n'explique.

    Escalier et non ligne droite entre deux points : le niveau ne descend
    pas continûment, il reste plat entre deux mouvements puis saute d'un
    coup. Relier les points en diagonale laisserait croire à une
    consommation régulière qui n'a jamais eu lieu.

    Deux repères horizontaux : la capacité (haut) et le seuil de 15 %, le
    même rouge que la jauge et que l'alerte « À réapprovisionner ».
    """

    LARGEUR = 260
    HAUTEUR = 130
    MARGE_G = 6
    MARGE_D = 6
    MARGE_H = 10
    MARGE_B = 16

    def __init__(self, master, nom: str):
        super().__init__(master, fg_color="transparent")
        # Même couleur en dur que `JaugeCuve`, et pour la même raison : ce
        # cadre est transparent, `_apply_appearance_mode` ne résout donc rien
        # d'utilisable pour le fond du canvas.
        self.canvas = tk.Canvas(self, width=self.LARGEUR, height=self.HAUTEUR,
                                highlightthickness=0, bd=0, bg="#1a1d23")
        self.canvas.pack()
        self._label_nom = ctk.CTkLabel(self, text=nom, text_color="#9ca3af",
                                       font=ctk.CTkFont(size=10),
                                       wraplength=self.LARGEUR)
        self._label_nom.pack(pady=(2, 0))
        self._points: list[dict] = []
        self._capacite = 0.0
        self._jours_fenetre = 30
        self.canvas.bind("<Configure>", lambda _e: self._dessiner())

    def maj(self, points: list[dict], capacite: float):
        self._points = points or []
        self._capacite = capacite or 0.0
        self._dessiner()

    def _dessiner(self):
        c = self.canvas
        c.delete("all")

        largeur = max(c.winfo_width(), self.LARGEUR)
        x0, x1 = self.MARGE_G, largeur - self.MARGE_D
        y0, y1 = self.MARGE_H, self.HAUTEUR - self.MARGE_B

        if not self._points or not self._capacite:
            c.create_text(largeur / 2, self.HAUTEUR / 2,
                          text=t("Pas encore de mouvement"),
                          fill="#6b7280", font=("Segoe UI", 9))
            return

        # Échelle verticale : toujours 0 -> capacité, jamais l'amplitude des
        # données. Une cuve qui oscille entre 900 et 1000 gal doit SE VOIR
        # pleine ; une échelle ajustée aux données la ferait paraître
        # dramatiquement basse.
        def y_pour(niveau):
            pct = max(0.0, min(1.0, niveau / self._capacite))
            return y1 - (y1 - y0) * pct

        # Repères : capacité et seuil de 15 %.
        c.create_line(x0, y0, x1, y0, fill="#3a3d45", dash=(2, 3))
        y_seuil = y_pour(self._capacite * 0.15)
        c.create_line(x0, y_seuil, x1, y_seuil, fill="#7f1d1d", dash=(2, 3))

        n = len(self._points)
        pas = (x1 - x0) / max(n - 1, 1)
        sommets = []
        for i, p in enumerate(self._points):
            x = x0 + pas * i
            y = y_pour(p.get("niveau") or 0)
            if sommets:
                # Palier : on reste au niveau précédent jusqu'à l'instant du
                # mouvement, puis on saute verticalement.
                sommets.append((x, sommets[-1][1]))
            sommets.append((x, y))

        dernier = self._points[-1].get("niveau") or 0
        pct = dernier / self._capacite if self._capacite else 0
        couleur = "#ef4444" if pct < 0.15 else ("#f59e0b" if pct < 0.40 else "#22c55e")

        # Aire sous la courbe, puis la courbe elle-même par-dessus.
        if len(sommets) >= 2:
            aire = [(x0, y1)] + sommets + [(sommets[-1][0], y1)]
            c.create_polygon([coord for point in aire for coord in point],
                             fill=couleur, outline="", stipple="gray25")
            c.create_line([coord for point in sommets for coord in point],
                          fill=couleur, width=2)
        else:
            x, y = sommets[0]
            c.create_line(x0, y, x1, y, fill=couleur, width=2)

        c.create_line(x0, y1, x1, y1, fill="#3a3d45")
        c.create_text(x0, y1 + 7,
                      text=t("il y a {n} j").format(n=self._jours_fenetre),
                      anchor="w", fill="#6b7280", font=("Segoe UI", 8))
        c.create_text(x1, y1 + 7, text=t("aujourd'hui"), anchor="e",
                      fill="#6b7280", font=("Segoe UI", 8))

    def set_fenetre(self, jours: int):
        """Fenêtre annoncée sous l'axe. Posée par l'écran, qui tient la
        constante — la courbe ne décide pas seule de ce qu'elle montre."""
        self._jours_fenetre = jours


class OverviewView(ctk.CTkFrame):
    # Fenêtre des courbes de niveau de cuve. 30 jours : assez long pour voir
    # une tendance de consommation, assez court pour que chaque mouvement
    # reste distinguable sur 260 pixels de large.
    JOURS_COURBE_CUVE = 30
    # Rafraîchissement des courbes : au plus une fois par quart d'heure en
    # l'absence de mouvement (le stock courant sert de détecteur), pour que
    # l'axe des dates avance sans rejouer l'historique à chaque cycle.
    PERIODE_COURBE_S = 900

    def __init__(self, master, api, get_ecarts=None, sector: str | None = None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        # Callback renvoyant le nombre d'écarts de réconciliation (admin seulement).
        self._get_ecarts = get_ecarts
        self._is_fuel = sector == "FUEL"
        self._backoff = REFRESH_MS
        self._job = None
        self._categorie_ouverte: str | None = None
        # Cuve sélectionnée (clic sur une jauge) : filtre les 3 graphiques
        # de détail sur elle seule au lieu du mélange national habituel.
        # Fuel uniquement — voir `_sur_clic_cuve`.
        self._cuve_ouverte: int | None = None
        self._last_data: dict | None = None
        self._build()
        # Pas de premier chargement ici : `main.py` construit la vue PUIS
        # l'affiche (`pack`), donc à cet instant elle n'est pas encore mappée
        # et `_poll()` se contenterait de se reprogrammer sans rien charger.
        # C'est `main.py::_show()` qui appelle `refresh()` juste après le
        # `pack()` — voir la note dans `refresh()`.

    def _build(self):
        entete = ctk.CTkFrame(self, fg_color="transparent")
        entete.pack(fill="x", padx=20, pady=(20, 4))
        ctk.CTkLabel(entete, text=t("Tableau de bord"),
                     font=ctk.CTkFont(size=22, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")
        self.status_label = ctk.CTkLabel(entete, text="", text_color="#22c55e",
                                         font=ctk.CTkFont(size=11))
        self.status_label.pack(side="right")

        # Alerte discrète : affichée seulement si la réconciliation détecte
        # un écart. Cliquable pour voir le détail (quel produit, quel
        # écart) — sans ça, il fallait interroger le serveur à la main pour
        # savoir de quoi il s'agissait.
        self.ecart_label = ctk.CTkLabel(entete, text="", text_color="#ef4444",
                                        font=ctk.CTkFont(size=11, underline=True),
                                        cursor="hand2")
        self.ecart_label.bind("<Button-1>", lambda e: self._afficher_details_ecarts())

        ctk.CTkButton(entete, text=t("Export Excel"), width=120,
                      fg_color="#1a1d23", hover_color="#2a2d35",
                      border_width=1, border_color="#2a2d35",
                      corner_radius=8, command=self._export_excel).pack(side="right", padx=(0, 8))
        ctk.CTkButton(entete, text=t("Export PDF"), width=120,
                      fg_color="#1a1d23", hover_color="#2a2d35",
                      border_width=1, border_color="#2a2d35",
                      corner_radius=8, command=self._rapport_mensuel).pack(side="right", padx=(0, 8))

        conteneur = ctk.CTkScrollableFrame(self, fg_color="transparent")
        conteneur.pack(fill="both", expand=True, padx=20, pady=(4, 20))

        # --- Cartes d'indicateurs ---
        cartes = ctk.CTkFrame(conteneur, fg_color="transparent")
        cartes.pack(fill="x", pady=(0, 12))
        for i in range(4):
            cartes.grid_columnconfigure(i, weight=1, uniform="kpi")

        self.carte_unites = KpiCard(cartes, t("Unités en stock"), "#3b82f6")
        self.carte_jour = KpiCard(cartes, t("Activité du jour"), "#6366f1",
                                  on_click=lambda: self._on_clic_documents_jour())
        self.carte_alertes = KpiCard(cartes, t("Alertes stock"), "#ef4444",
                                     on_click=self._on_clic_alertes)
        self.carte_couverture = KpiCard(cartes, t("Couverture < 7j"), "#f97316",
                                        on_click=self._on_clic_couverture)
        for i, carte in enumerate((self.carte_unites, self.carte_jour,
                                    self.carte_alertes, self.carte_couverture)):
            carte.grid(row=0, column=i, padx=4, sticky="nsew")

        cartes2 = ctk.CTkFrame(conteneur, fg_color="transparent")
        cartes2.pack(fill="x", pady=(0, 12))
        for i in range(3):
            cartes2.grid_columnconfigure(i, weight=1, uniform="kpi2")

        self.carte_recu = KpiCard(cartes2, t("Reçu (30 j)"), "#22c55e",
                                  on_click=lambda: self._on_clic_documents("RECEIVING"))
        self.carte_livre = KpiCard(cartes2, t("Livré (30 j)"), "#f59e0b",
                                   on_click=lambda: self._on_clic_documents("DELIVERY"))
        if self._is_fuel:
            # Fuel ne connaît pas le retour de carburant : la carte n'aurait
            # jamais rien d'autre que zéro.
            self.carte_retours = None
            cartes_ligne2 = (self.carte_recu, self.carte_livre)
        else:
            self.carte_retours = KpiCard(cartes2, t("Retours (30 j)"), "#3b82f6",
                                         on_click=lambda: self._on_clic_documents("RETURN"))
            cartes_ligne2 = (self.carte_recu, self.carte_livre, self.carte_retours)
        for i, carte in enumerate(cartes_ligne2):
            carte.grid(row=0, column=i, padx=4, sticky="nsew")

        # --- Totaux par carburant (secteur FUEL) ---
        # Rijkaard supervise tout le pays : la première chose qu'il doit voir
        # est le total PAR CARBURANT (Diesel, Gasoline...), jamais mélangés
        # entre eux — puis, juste en dessous, le détail cuve par cuve (une
        # cuve = un produit, ex. « Diesel — WH Central », « Gasoline —
        # Canapé-Vert »). Basé sur `data["categories"]`, déjà calculé par
        # /dashboard (catégorie = carburant pour ce secteur).
        self._cadre_carburants = None
        self._cartes_carburant: dict[str, KpiCard] = {}
        if self._is_fuel:
            self._cadre_carburants = ctk.CTkFrame(conteneur, fg_color="transparent")
            self._cadre_carburants.pack(fill="x", pady=(0, 12))

        # --- Jauges de cuve (secteur FUEL) ---
        # Un coup d'œil sur le niveau réel de chaque cuve, sans ouvrir Produits
        # ni Stock : c'est l'information qu'Orelus et Rijkaard regardent en
        # premier chaque matin.
        self._cadre_cuves = None
        self._jauges: dict[int, "JaugeCuve"] = {}
        self._courbes: dict[int, "CourbeNiveauCuve"] = {}
        self._courbes_stock: dict[int, float] = {}
        self._courbes_chargees: dict[int, float] = {}
        if self._is_fuel:
            self._cadre_cuves = ctk.CTkFrame(conteneur, fg_color="#1a1d23",
                                             corner_radius=10, border_width=1,
                                             border_color="#2a2d35")
            self._cadre_cuves.pack(fill="x", pady=(0, 12))
            ctk.CTkLabel(self._cadre_cuves, text=t("⛽  Niveau des cuves"),
                        font=ctk.CTkFont(size=13, weight="bold"),
                        text_color="#f0f0f0").pack(anchor="w", padx=14, pady=(12, 4))
            self._jauges_frame = ctk.CTkFrame(self._cadre_cuves, fg_color="transparent")
            self._jauges_frame.pack(fill="x", padx=14, pady=(0, 14))
            self._jauges_vide_label = ctk.CTkLabel(
                self._jauges_frame,
                text=t("Aucune cuve configurée — pose une capacité de cuve "
                       "sur un produit dans Produits."),
                text_color="#6b7280", font=ctk.CTkFont(size=11))
            self._jauges_vide_label.pack(anchor="w")

            # Évolution : la jauge dit où on en est, la courbe dit à quelle
            # vitesse on y est arrivé. Une cuve qui se vide deux fois plus
            # vite que d'habitude ne se voit sur aucune jauge.
            entete_courbes = ctk.CTkFrame(self._cadre_cuves, fg_color="transparent")
            entete_courbes.pack(fill="x", padx=14, pady=(4, 4))
            self._titre_courbes = ctk.CTkLabel(
                entete_courbes,
                text=t("📉  Évolution du niveau ({n} derniers jours)").format(
                    n=self.JOURS_COURBE_CUVE),
                font=ctk.CTkFont(size=13, weight="bold"),
                text_color="#f0f0f0")
            self._titre_courbes.pack(side="left")

            # Fenêtre choisie par l'opérateur : 30 jours par défaut, mais une
            # cuve qui se vide vite se lit mieux sur 7 j, une tendance lente
            # sur 90 j. Le bouton actif reste visuellement plein.
            periode_frame = ctk.CTkFrame(entete_courbes, fg_color="transparent")
            periode_frame.pack(side="right")
            self._btns_periode: dict[int, ctk.CTkButton] = {}
            for jours, libelle in ((7, "7j"), (30, "30j"), (90, "90j")):
                btn = ctk.CTkButton(
                    periode_frame, text=libelle, width=44, height=24,
                    corner_radius=6, font=ctk.CTkFont(size=11),
                    fg_color="#3b82f6" if jours == self.JOURS_COURBE_CUVE else "#1a1d23",
                    hover_color="#2a2d35", text_color="#f0f0f0",
                    command=lambda j=jours: self._changer_fenetre_courbe(j))
                btn.pack(side="left", padx=(4, 0))
                self._btns_periode[jours] = btn

            self._courbes_frame = ctk.CTkFrame(self._cadre_cuves, fg_color="transparent")
            self._courbes_frame.pack(fill="x", padx=14, pady=(0, 14))

        # --- Graphiques ---
        graphiques = ctk.CTkFrame(conteneur, fg_color="transparent")
        graphiques.pack(fill="both", expand=True)
        graphiques.grid_columnconfigure(0, weight=3, uniform="g")
        graphiques.grid_columnconfigure(1, weight=2, uniform="g")

        # Fuel n'a pas de catégorie/région qui vaille (une seule cuve, pas de
        # région) : les trois graphiques sont repris pour montrer QUI/QUOI
        # consomme le carburant — véhicule, projet, receveur — plutôt que
        # produit/catégorie/région, qui n'ont aucun sens pour un secteur à un
        # seul article.
        self.graph_categories = BarChart(
            graphiques,
            t("Consommation par receveur (30 j)") if self._is_fuel else t("Stock par catégorie"),
            on_click=None if self._is_fuel else self._sur_clic_categorie)
        self.graph_categories.grid(row=0, column=0, sticky="nsew", padx=(0, 6), pady=(0, 12))

        self.graph_regions = BarChart(
            graphiques,
            t("Par projet (30 j)") if self._is_fuel else t("Par région (30 j)"))
        self.graph_regions.grid(row=0, column=1, sticky="nsew", padx=(6, 0), pady=(0, 12))

        self.graph_top = BarChart(
            graphiques,
            t("Top véhicules (30 j)") if self._is_fuel else t("Top sorties (30 j)"))
        self.graph_top.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 12))

        # Quatrième graphique, Fuel seulement : les trois ci-dessus mélangent
        # les sites sans le dire sur la vue nationale de l'admin (Rijkaard) —
        # rien ne distingue une consommation de WH Central de Canapé-Vert.
        # Demandé par Idy509 le 2026-09-09 après avoir vu ce mélange à
        # l'écran. Prend la place des alertes dans la grille 2×2 du haut ;
        # les alertes passent alors en pleine largeur juste en dessous,
        # plutôt que d'ajouter une troisième colonne étroite.
        self.graph_site = None
        if self._is_fuel:
            self.graph_site = BarChart(graphiques, t("Par site (30 j)"))
            self.graph_site.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(0, 12))

        cadre_alertes = ctk.CTkFrame(graphiques, corner_radius=10, fg_color="#1a1d23",
                                     border_width=1, border_color="#2a2d35")
        if self._is_fuel:
            cadre_alertes.grid(row=2, column=0, columnspan=2, sticky="nsew",
                               padx=0, pady=(0, 12))
        else:
            cadre_alertes.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(0, 12))

        entete_alertes = ctk.CTkFrame(cadre_alertes, fg_color="transparent")
        entete_alertes.pack(fill="x", padx=14, pady=(12, 2))
        ctk.CTkLabel(entete_alertes, text=t("⚠  À réapprovisionner"),
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")
        self.badge_alertes = ctk.CTkLabel(entete_alertes, text="",
                                           font=ctk.CTkFont(size=11, weight="bold"),
                                           text_color="#ef4444")
        self.badge_alertes.pack(side="right")

        alerte_scroll_frame = ctk.CTkFrame(cadre_alertes, fg_color="transparent")
        alerte_scroll_frame.pack(fill="both", expand=True, padx=14, pady=(4, 12))

        self.canvas_alertes = tk.Canvas(alerte_scroll_frame, highlightthickness=0, bd=0,
                                         height=200)
        self._scroll_alertes = ttk.Scrollbar(alerte_scroll_frame, orient="vertical",
                                              command=self.canvas_alertes.yview)
        self.canvas_alertes.configure(yscrollcommand=self._scroll_alertes.set)
        self.canvas_alertes.pack(side="left", fill="both", expand=True)

        def _on_mousewheel_alertes(event):
            self.canvas_alertes.yview_scroll(int(-1 * (event.delta / 120)), "units")
        self.canvas_alertes.bind("<MouseWheel>", _on_mousewheel_alertes)
        self.canvas_alertes.bind("<Configure>", lambda e: self._dessiner_alertes())
        self._alertes_data: list[dict] = []
        # Nombre total d'alertes côté serveur (la liste reçue est plafonnée).
        self._alertes_total: int = 0

        # --- Couverture stock (Consumables/FON) / Dernières transactions (Fuel) ---
        # Pour Fuel, la « Couverture de stock » réservait 6 lignes de hauteur
        # pour au mieux UNE ligne réelle (un site n'a jamais qu'une poignée de
        # cuves) — un grand vide, en plus d'une info déjà portée par
        # l'autonomie affichée sur chaque jauge (`JaugeCuve.set_autonomie`).
        # Remplacé pour ce secteur par un fil d'activité (`GET /dashboard`,
        # champ `dernieres_transactions`, scopé au SITE de l'appelant côté
        # serveur — jamais la vue nationale de l'admin sur cet écran-là).
        cadre_couverture = ctk.CTkFrame(graphiques, corner_radius=10, fg_color="#1a1d23",
                                         border_width=1, border_color="#2a2d35")
        cadre_couverture.grid(row=3 if self._is_fuel else 2, column=0, columnspan=2,
                              sticky="nsew", padx=0, pady=(0, 12))

        entete_couv = ctk.CTkFrame(cadre_couverture, fg_color="transparent")
        entete_couv.pack(fill="x", padx=14, pady=(12, 2))
        ctk.CTkLabel(entete_couv,
                     text=t("🕘  Dernières transactions") if self._is_fuel
                          else t("📉  Couverture de stock"),
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")
        self.badge_couverture = ctk.CTkLabel(entete_couv, text="",
                                              font=ctk.CTkFont(size=11, weight="bold"),
                                              text_color="#f97316")
        self.badge_couverture.pack(side="right")

        couv_table_frame = ctk.CTkFrame(cadre_couverture, fg_color="transparent")
        couv_table_frame.pack(fill="both", expand=True, padx=14, pady=(4, 12))

        if self._is_fuel:
            cols_couv = ("date", "type", "vehicule_receveur", "projet", "quantite")
            self.tree_couverture = ttk.Treeview(couv_table_frame, columns=cols_couv,
                                                  show="headings", height=6)
            for col, texte, larg in [("date", t("Date"), 130), ("type", t("Type"), 90),
                                       ("vehicule_receveur", t("Véhicule / Receveur"), 200),
                                       ("projet", t("Projet"), 130),
                                       ("quantite", t("Qté (gal)"), 90)]:
                self.tree_couverture.heading(col, text=texte,
                                              command=lambda c=col: _sort_treeview(self.tree_couverture, c))
                self.tree_couverture.column(col, width=larg, anchor="w")
        else:
            cols_couv = ("sku", "nom", "stock", "conso_jour", "jours")
            self.tree_couverture = ttk.Treeview(couv_table_frame, columns=cols_couv,
                                                  show="headings", height=6)
            for col, texte, larg in [("sku", "SKU", 110), ("nom", t("Produit"), 280),
                                       ("stock", t("Stock"), 90),
                                       ("conso_jour", t("Conso/jour"), 90),
                                       ("jours", t("Jours restants"), 110)]:
                self.tree_couverture.heading(col, text=texte,
                                              command=lambda c=col: _sort_treeview(self.tree_couverture, c))
                self.tree_couverture.column(col, width=larg, anchor="w")
        self.tree_couverture.tag_configure("critique", foreground="#ef4444")
        self.tree_couverture.tag_configure("alerte", foreground="#f59e0b")
        self.tree_couverture.pack(side="left", fill="both", expand=True)
        sb_couv = ttk.Scrollbar(couv_table_frame, orient="vertical",
                                 command=self.tree_couverture.yview)
        self.tree_couverture.configure(yscrollcommand=sb_couv.set)
        sb_couv.pack(side="right", fill="y")

    # ------------------------------------------------------------------ données

    def _poll(self):
        if not self.winfo_exists():
            return
        if not self.winfo_ismapped():
            self._replanifier()
            return
        run_async(self, self.api.get_dashboard, self._on_data, self._on_error)

    def refresh(self):
        """Charge les indicateurs immédiatement (affichage de la vue, F5).

        Contrairement à `_poll()`, ne teste pas `winfo_ismapped()` : appelée
        par `main.py::_show()` juste après le `pack()`, Tk n'a pas encore
        mappé le widget (le mapping se fait dans la file « idle »), alors que
        c'est bien la vue que l'opérateur regarde.
        """
        if not self.winfo_exists():
            return
        self._poll_immediat()

    def _sur_clic_categorie(self, categorie: str | None):
        """Ouvre le détail d'une catégorie (sous-totaux par groupe), ou revient à la racine."""
        self._categorie_ouverte = categorie
        if categorie is None:
            self.graph_categories.set_mode_detail(None)
            self._poll_immediat()
            return
        run_async(self, lambda: self.api.get_dashboard_categorie(categorie),
                  self._on_detail, self._on_error)

    def _on_detail(self, detail):
        categorie = detail["categorie"]
        total = detail["total"]
        self.graph_categories.set_mode_detail(
            t("{categorie} — {unites} unités, {refs} réf.").format(
                categorie=categorie, unites=_fmt(total["unites"]),
                refs=total["references_total"])
        )
        self.graph_categories.set_donnees(
            [(g["groupe"], g["unites"]) for g in detail["groupes"]]
        )

    def _sur_clic_cuve(self, product_id: int):
        """Filtre les 3 graphiques de détail (receveur/projet/véhicule) sur
        UNE seule cuve. Cliquer la même cuve une seconde fois revient au
        mélange national — demandé par Idy509 le 2026-09-09."""
        if self._cuve_ouverte == product_id:
            self._cuve_ouverte = None
            for jauge in self._jauges.values():
                jauge.set_selectionnee(False)
            for graphe in (self.graph_categories, self.graph_regions, self.graph_top):
                graphe.set_mode_detail(None)
            self._poll_immediat()
            return
        self._cuve_ouverte = product_id
        for pid, jauge in self._jauges.items():
            jauge.set_selectionnee(pid == product_id)
        run_async(self, lambda: self.api.get_dashboard_cuve(product_id),
                  self._on_cuve_detail, self._on_error)

    def _on_cuve_detail(self, detail):
        # Réponse d'un clic déjà abandonné (cuve désélectionnée ou une autre
        # choisie entre-temps) : ne jamais l'appliquer, elle écraserait
        # l'état courant avec des données périmées.
        if self._cuve_ouverte != detail["product_id"]:
            return
        nom = detail["name"]
        self.graph_categories.set_mode_detail(f"{t('Consommation')} — {nom}")
        self.graph_categories.set_donnees(
            [(r["receveur"], r["quantite"]) for r in detail["par_receveur"]]
        )
        self.graph_regions.set_mode_detail(f"{t('Par projet')} — {nom}")
        self.graph_regions.set_donnees(
            [(p["projet"], p["quantite"]) for p in detail["par_projet"]]
        )
        self.graph_top.set_mode_detail(f"{t('Top véhicules')} — {nom}")
        self.graph_top.set_donnees(
            [(f"{v['plaque']} ({v['modele']})" if v.get("modele") else v["plaque"],
              v["quantite"])
             for v in detail["top_vehicules"]]
        )

    def _poll_immediat(self):
        # Annuler le rafraîchissement automatique déjà programmé : sans cela,
        # chaque appel immédiat ajoute un cycle de polling supplémentaire
        # (deux requêtes en parallèle pour la même donnée).
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        run_async(self, self.api.get_dashboard, self._on_data, self._on_error)
        if self._is_fuel:
            self._charger_cuves()

    _COULEURS_CARBURANT = ["#3b82f6", "#f59e0b", "#a855f7", "#22c55e", "#ef4444"]

    def _maj_cartes_carburant(self, categories: list[dict]):
        """Une carte totale par carburant (catégorie) — jamais mélangés entre
        eux, contrairement au total générique "Unités en stock"."""
        if self._cadre_carburants is None:
            return
        vus = set()
        for i, cat in enumerate(categories):
            nom = cat["categorie"]
            vus.add(nom)
            couleur = self._COULEURS_CARBURANT[i % len(self._COULEURS_CARBURANT)]
            if nom in self._cartes_carburant:
                carte = self._cartes_carburant[nom]
            else:
                carte = KpiCard(self._cadre_carburants, nom, couleur)
                self._cartes_carburant[nom] = carte
            carte.set(f"{_fmt(cat['unites'])} gal", couleur)
            # `en_rupture` (cuve totalement vide) prime sur `sous_seuil` (cuve
            # non vide mais sous le seuil de réapprovisionnement, 15 % de la
            # capacité par défaut) : sinon une cuve à 170/1300 gal (13 %,
            # ROUGE sur sa jauge) affichait « Approvisionné » ici, faute de
            # connaître autre chose que la rupture totale.
            if cat.get("en_rupture"):
                carte.set_trend(
                    t("{n} cuve(s) à sec").format(n=cat["en_rupture"]), positive=False)
            elif cat.get("sous_seuil"):
                carte.set_trend(
                    t("{n} cuve(s) sous le seuil").format(n=cat["sous_seuil"]), positive=False)
            else:
                carte.set_trend(t("Approvisionné"), positive=True)

        for nom in list(self._cartes_carburant):
            if nom not in vus:
                self._cartes_carburant.pop(nom).destroy()

        for i in range(len(self._cartes_carburant)):
            self._cadre_carburants.grid_columnconfigure(i, weight=1, uniform="carb")
        for i, carte in enumerate(self._cartes_carburant.values()):
            carte.grid(row=0, column=i, padx=4, sticky="nsew")

    def _charger_cuves(self):
        """Peuple les jauges de cuve à partir des produits à capacité limitée.

        `all_sites=True` : le Tableau de bord reste la vue GLOBALE d'un
        admin (Rijkaard doit voir la cuve d'Orelus autant que les siennes) —
        contrairement à Stock/Réception/Expédition/Jaugeage, scopés au site
        de l'appelant même pour un admin. Ignoré côté serveur pour un compte
        non-admin, qui ne voit donc toujours que son propre site ici.
        """
        run_async(self, lambda: self.api.get_products(all_sites=True),
                 self._on_cuves_ok, lambda _e: None)

    def _on_cuves_ok(self, produits):
        if not self.winfo_exists() or self._cadre_cuves is None:
            return
        cuves = [p for p in produits if p.get("tank_capacity")]
        if not cuves:
            self._jauges_vide_label.pack(anchor="w")
            for jauge in self._jauges.values():
                jauge.destroy()
            self._jauges.clear()
            return
        self._jauges_vide_label.pack_forget()

        vus = set()
        for p in cuves:
            vus.add(p["id"])
            if p["id"] in self._jauges:
                self._jauges[p["id"]].maj(p["current_stock"], p["tank_capacity"])
            else:
                jauge = JaugeCuve(self._jauges_frame, _libelle_cuve(p),
                                  p["current_stock"], p["tank_capacity"],
                                  on_click=lambda pid=p["id"]: self._sur_clic_cuve(pid))
                jauge.pack(side="left", padx=(0, 16))
                jauge.set_selectionnee(p["id"] == self._cuve_ouverte)
                self._jauges[p["id"]] = jauge

            if p["id"] not in self._courbes:
                courbe = CourbeNiveauCuve(self._courbes_frame, _libelle_cuve(p))
                courbe.set_fenetre(self.JOURS_COURBE_CUVE)
                courbe.pack(side="left", padx=(0, 16))
                self._courbes[p["id"]] = courbe
            if self._courbe_a_recharger(p["id"], p["current_stock"]):
                self._charger_courbe(p["id"], p["tank_capacity"])

        # Un produit dont la capacité a été retirée depuis (ou archivé) ne
        # doit pas laisser une jauge fantôme affichée indéfiniment.
        for pid in list(self._jauges):
            if pid not in vus:
                self._jauges.pop(pid).destroy()
        for pid in list(self._courbes):
            if pid not in vus:
                self._courbes.pop(pid).destroy()
                self._courbes_stock.pop(pid, None)
                self._courbes_chargees.pop(pid, None)

    def _courbe_a_recharger(self, product_id: int, stock: float) -> bool:
        """Vrai s'il vaut la peine de redemander l'historique de cette cuve.

        Le tableau de bord se rafraîchit toutes les 30 secondes ; une courbe
        sur 30 JOURS ne change qu'à deux occasions : un mouvement (le stock
        courant bouge alors aussi) ou le simple passage du temps, qui décale
        l'axe. Redemander l'historique complet à chaque cycle ferait balayer
        `document_lines` pour un dessin identique — trois fois par cycle avec
        trois cuves.
        """
        maintenant_s = time.monotonic()
        stock_connu = self._courbes_stock.get(product_id)
        charge_le = self._courbes_chargees.get(product_id, 0.0)
        if (stock_connu is not None
                and abs(stock - stock_connu) < 1e-9
                and maintenant_s - charge_le < self.PERIODE_COURBE_S):
            return False
        self._courbes_stock[product_id] = stock
        self._courbes_chargees[product_id] = maintenant_s
        return True

    def _charger_courbe(self, product_id: int, capacite: float):
        """Historique de niveau d'UNE cuve, appel indépendant des autres.

        En silence si l'appel échoue : la courbe est un complément de la
        jauge, elle ne doit jamais empêcher le tableau de bord de s'afficher
        (même règle que le reste de cet écran).
        """
        def _ok(data, pid=product_id, cap=capacite):
            courbe = self._courbes.get(pid)
            if courbe is not None and courbe.winfo_exists():
                courbe.maj(data.get("points", []), data.get("tank_capacity") or cap)

        run_async(self,
                  lambda: self.api.get_niveau_cuve(product_id,
                                                   jours=self.JOURS_COURBE_CUVE),
                  _ok, lambda _e: None)

    def _changer_fenetre_courbe(self, jours: int):
        """Change la période affichée par les courbes de niveau (7/30/90 j).

        Vide les caches de fraîcheur (`_courbes_stock`/`_courbes_chargees`) :
        `_courbe_a_recharger` les traite comme jamais chargées et redemande
        aussitôt l'historique à la nouvelle fenêtre — sans ça, le stock
        inchangé aurait fait croire à une courbe déjà à jour.
        """
        self.JOURS_COURBE_CUVE = jours
        for j, btn in self._btns_periode.items():
            btn.configure(fg_color="#3b82f6" if j == jours else "#1a1d23")
        self._titre_courbes.configure(
            text=t("📉  Évolution du niveau ({n} derniers jours)").format(n=jours))
        for courbe in self._courbes.values():
            courbe.set_fenetre(jours)
        self._courbes_stock.clear()
        self._courbes_chargees.clear()
        self._charger_cuves()

    def _on_data(self, data):
        self._apply_data(data)

    def _apply_data(self, data):
        self._last_data = data
        resume = data["resume"]
        mouvements = data["mouvements"]

        self.carte_unites.set(_fmt(resume["unites_totales"]))
        if self._is_fuel:
            self._maj_cartes_carburant(data.get("categories", []))
        jour = data.get("activite_jour", {})
        bons_jour = jour.get("bons_jour", 0)
        self.carte_jour.set(str(bons_jour), "#6366f1" if bons_jour else "#9ca3af")
        rec_j = jour.get("receptions_jour", 0)
        liv_j = jour.get("livraisons_jour", 0)
        if bons_jour:
            self.carte_jour.set_trend(
                t("{r} réception(s), {l} livraison(s)").format(r=rec_j, l=liv_j),
                positive=True)
        else:
            self.carte_jour.set_trend(t("Aucun mouvement"), positive=False)
        self.carte_recu.set(_fmt(mouvements["recu_30j"]), "#22c55e")
        self.carte_livre.set(_fmt(mouvements["livre_30j"]), "#f59e0b")
        if self.carte_retours is not None:
            self.carte_retours.set(_fmt(mouvements.get("retours_30j", 0)), "#3b82f6")

        # Tendances par rapport aux 30 jours précédents
        prev = data.get("mouvements_prev", {})
        tendances = [
            (self.carte_recu, "recu_30j", "recu_prev"),
            (self.carte_livre, "livre_30j", "livre_prev"),
        ]
        if self.carte_retours is not None:
            tendances.append((self.carte_retours, "retours_30j", "retours_prev"))
        for carte, cle_cur, cle_prev in tendances:
            cur_val = mouvements.get(cle_cur, 0)
            prev_val = prev.get(cle_prev, 0)
            if prev_val > 0:
                pct = ((cur_val - prev_val) / prev_val) * 100
                carte.set_trend(f"{pct:+.0f}% vs M-1", positive=pct >= 0)
            elif cur_val > 0:
                carte.set_trend("+100% vs M-1", positive=True)
            else:
                carte.set_trend("")

        # Compteurs exacts du serveur : la liste `alertes` est tronquée à 50
        # lignes pour l'affichage, elle ne peut pas servir de KPI.
        nb_ruptures = int(resume.get("en_rupture", 0) or 0)
        nb_sous_seuil = int(resume.get("sous_seuil", 0) or 0)
        nb_alertes = nb_ruptures + nb_sous_seuil
        self.carte_alertes.set(str(nb_alertes), "#ef4444" if nb_alertes else "#22c55e")

        nb_couv = data.get("nb_couverture_critique", 0)
        self.carte_couverture.set(str(nb_couv), "#f97316" if nb_couv else "#22c55e")
        if nb_couv:
            couv = data.get("couverture", [])
            if couv:
                pire = couv[0]
                self.carte_couverture.set_trend(
                    f"{pire['sku']} : {pire['jours_restants']:.0f}j", positive=False)
        else:
            self.carte_couverture.set_trend(t("Tous > 7 jours"), positive=True)

        if self._is_fuel:
            # Ne pas écraser le détail si l'utilisateur a sélectionné une
            # cuve (clic sur une jauge) : on le rafraîchit plutôt, même
            # principe que `_categorie_ouverte` juste en dessous.
            if self._cuve_ouverte is None:
                # Suffixe " · site" ajouté SEULEMENT si plusieurs sites
                # distincts apparaissent dans la même réponse : un compte
                # scopé à un site (Orelus) n'en voit jamais qu'un, le
                # suffixe n'y ajouterait rien. Sur la vue nationale de
                # l'admin, un même receveur ou projet peut exister à deux
                # sites sans se distinguer autrement (constaté par Idy509 le
                # 2026-09-09).
                def _libelles_avec_site(lignes, cle):
                    sites = {l.get("site") for l in lignes if l.get("site")}
                    multi = len(sites) > 1
                    return [
                        (f"{l[cle]} · {l['site']}" if multi and l.get("site") else l[cle],
                         l["quantite"])
                        for l in lignes
                    ]

                self.graph_categories.set_donnees(
                    _libelles_avec_site(data.get("par_receveur", []), "receveur")
                )
                self.graph_regions.set_donnees(
                    _libelles_avec_site(data.get("par_projet", []), "projet")
                )
                vehicules = data.get("top_vehicules", [])
                sites_vehicules = {v.get("site") for v in vehicules if v.get("site")}
                multi_vehicules = len(sites_vehicules) > 1
                self.graph_top.set_donnees([
                    (
                        (f"{v['plaque']} ({v['modele']})" if v.get("modele") else v["plaque"])
                        + (f" · {v['site']}" if multi_vehicules and v.get("site") else ""),
                        v["quantite"],
                    )
                    for v in vehicules
                ])
            else:
                run_async(self, lambda: self.api.get_dashboard_cuve(self._cuve_ouverte),
                          self._on_cuve_detail, lambda e: None)
            if self.graph_site is not None:
                self.graph_site.set_donnees(
                    [(s["site"], s["quantite"]) for s in data.get("par_site", [])]
                )
        else:
            # Ne pas écraser le détail si l'utilisateur a ouvert une catégorie.
            if self._categorie_ouverte is None:
                self.graph_categories.set_donnees(
                    [(c["categorie"], c["unites"]) for c in data["categories"]]
                )
            else:
                run_async(self, lambda: self.api.get_dashboard_categorie(self._categorie_ouverte),
                          self._on_detail, lambda e: None)

            self.graph_regions.set_donnees(
                [(r["region"], r["quantite"]) for r in data["par_region"]]
            )
            self.graph_top.set_donnees(
                [(ligne["category"] or ligne["sku"], ligne["quantite"])
                 for ligne in data["top_sorties"][:5]]
            )

        # Toujours lue : l'autonomie par cuve (plus bas, jauges Fuel) en a
        # besoin même quand le tableau affiché n'est pas la couverture.
        couverture = data.get("couverture", [])
        self.tree_couverture.delete(*self.tree_couverture.get_children())
        if self._is_fuel:
            transactions = data.get("dernieres_transactions", [])
            for tr in transactions:
                date_str = (tr.get("created_at") or "")[:16].replace("T", " ")
                type_str = DocumentsPopup.TYPES_FR.get(tr["type"], tr["type"])
                vehicule_receveur = tr.get("vehicle_plate") or tr.get("receveur") or "—"
                quantite = tr.get("quantite")
                self.tree_couverture.insert("", "end", values=(
                    date_str, type_str, vehicule_receveur,
                    tr.get("project") or "—",
                    _fmt(quantite) if quantite is not None else "—",
                ))
            if transactions:
                self.badge_couverture.configure(
                    text=t("{n} mouvement(s)").format(n=len(transactions)))
            else:
                self.badge_couverture.configure(text=t("Aucune donnée"),
                                                text_color="#6b7280")
        else:
            for c in couverture:
                jours = c["jours_restants"]
                if jours < 7:
                    tag = ("critique",)
                elif jours < 14:
                    tag = ("alerte",)
                else:
                    tag = ()
                self.tree_couverture.insert("", "end", tags=tag, values=(
                    c["sku"], c["name"], _fmt(c["current_stock"]),
                    f"{c['conso_jour']:.1f}", f"{jours:.0f}",
                ))
            if couverture:
                self.badge_couverture.configure(
                    text=t("{n} produit(s)").format(n=len(couverture)))
            else:
                self.badge_couverture.configure(text=t("Aucune donnée"),
                                                text_color="#6b7280")

        if self._is_fuel:
            # Autonomie par cuve, lue dans la même couverture que le tableau
            # ci-dessus : pas de conso récente = pas d'entrée = None (jauge
            # sans label plutôt qu'une fausse autonomie infinie).
            couv_par_produit = {
                c["product_id"]: c["jours_restants"] for c in couverture
                if "product_id" in c
            }
            for pid, jauge in self._jauges.items():
                jauge.set_autonomie(couv_par_produit.get(pid))

        alertes = sorted(data["alertes"],
                         key=lambda a: a["current_stock"] / max(a["min_stock"], 0.01))
        self._alertes_data = alertes
        # Total exact (serveur) vs nombre de lignes réellement affichées.
        self._alertes_total = max(nb_alertes, len(alertes))
        if self._alertes_total:
            txt = t("{n} produit(s)").format(n=self._alertes_total)
            if nb_ruptures:
                txt += " · " + t("{n} en rupture").format(n=nb_ruptures)
            if self._alertes_total > len(alertes):
                txt += " · " + t("{n} affichés").format(n=len(alertes))
            self.badge_alertes.configure(
                text=txt, text_color="#ef4444" if nb_ruptures else "#f59e0b")
        else:
            self.badge_alertes.configure(text=t("Tout OK"), text_color="#22c55e")
        self._dessiner_alertes()

        self._backoff = REFRESH_MS
        self.status_label.configure(text=t("●  En direct"), text_color="#22c55e")
        self._maj_ecarts()
        self._replanifier()

    def _maj_ecarts(self):
        """Affiche l'alerte de réconciliation, uniquement s'il y a des écarts."""
        if not self._get_ecarts:
            return
        try:
            nombre = int(self._get_ecarts() or 0)
        except Exception:
            return
        if nombre > 0:
            self.ecart_label.configure(
                text=t("⚠  {n} écart(s) de stock détecté(s) — cliquer pour "
                       "le détail").format(n=nombre))
            if not self.ecart_label.winfo_ismapped():
                self.ecart_label.pack(side="right", padx=(0, 12))
        elif self.ecart_label.winfo_ismapped():
            self.ecart_label.pack_forget()

    def _afficher_details_ecarts(self):
        """Ouvre le détail des écarts de réconciliation au clic sur l'alerte.

        Requête à la demande plutôt que données mises en cache : l'alerte
        peut rester affichée un moment, le détail doit refléter l'état
        actuel au moment où l'opérateur clique, pas celui du dernier
        contrôle silencieux.
        """
        run_async(self, self.api.get_stock_reconciliation,
                  self._on_details_ecarts_ok, self._on_details_ecarts_erreur)

    def _on_details_ecarts_erreur(self, exc):
        messagebox.showerror(t("Écarts de stock"), str(exc))

    def _on_details_ecarts_ok(self, data):
        EcartsDialog(self, data)

    def _dessiner_alertes(self):
        c = self.canvas_alertes
        c.delete("all")
        c.configure(bg="#1a1d23")
        if not self._alertes_data:
            c.create_text(12, 20, anchor="w", text=t("Aucune alerte"), fill="#6b7280",
                          font=("Segoe UI", 11))
            self._scroll_alertes.pack_forget()
            c.configure(scrollregion=(0, 0, 0, 50))
            return

        largeur = c.winfo_width() or 400
        ligne_h = 24
        for i, a in enumerate(self._alertes_data):
            y = i * ligne_h
            stock = a["current_stock"]
            seuil = a["min_stock"]
            nom = a.get("name") or a["sku"]
            max_nom = max((largeur - 140) // 7, 10)
            if len(nom) > max_nom:
                nom = nom[:max_nom - 1] + "…"

            if stock <= 0:
                couleur = "#ef4444"
                tag = t("RUPTURE")
            elif seuil > 0 and stock <= seuil / 2:
                couleur = "#f59e0b"
                tag = t("CRITIQUE")
            else:
                couleur = "#3b82f6"
                tag = t("BAS")

            c.create_oval(6, y + 7, 14, y + 15, fill=couleur, outline="")
            c.create_text(20, y + ligne_h / 2, anchor="w", text=nom, fill="#e5e7eb",
                          font=("Segoe UI", 10))
            detail = f"{_fmt(stock)}/{_fmt(seuil)}"
            c.create_text(largeur - 70, y + ligne_h / 2, anchor="e",
                          text=detail, fill="#9ca3af", font=("Segoe UI", 9))
            c.create_text(largeur - 6, y + ligne_h / 2, anchor="e",
                          text=tag, fill=couleur, font=("Segoe UI", 9, "bold"))

        nb_lignes = len(self._alertes_data)
        hauteur_totale = max(nb_lignes * ligne_h, 50)
        # Le serveur ne renvoie que les 50 premières alertes : le dire, sinon
        # le magasinier croit avoir la liste complète sous les yeux.
        if self._alertes_total > nb_lignes:
            c.create_text(
                12, nb_lignes * ligne_h + 12, anchor="w",
                text=t("{n} premiers sur {total} — voir la vue Produits pour "
                       "la liste complète").format(
                           n=nb_lignes, total=self._alertes_total),
                fill="#9ca3af", font=("Segoe UI", 9, "italic"))
            hauteur_totale = nb_lignes * ligne_h + 26
        c.configure(scrollregion=(0, 0, largeur, hauteur_totale))
        if len(self._alertes_data) > 8:
            self._scroll_alertes.pack(side="right", fill="y")
        else:
            self._scroll_alertes.pack_forget()

    def _rapport_mensuel(self):
        if not self._last_data:
            messagebox.showinfo(t("Rapport"),
                                t("Données non disponibles. Attends le chargement."))
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".pdf", filetypes=[("PDF", "*.pdf")],
            initialfile=f"Talkpool_Warehouse_Report_{date.today().strftime('%Y-%m-%d')}.pdf")
        if not path:
            return

        data = self._last_data
        resume = data["resume"]
        mouvements = data["mouvements"]
        jour = data.get("activite_jour", {})
        alertes = data["alertes"]
        top = data["top_sorties"]
        categories = data["categories"]
        par_region = data["par_region"]
        couverture = data.get("couverture", [])
        today = date.today()
        debut = today - timedelta(days=30)

        pdf = PdfAvecPiedDePage(format="A4")
        font = setup_pdf_fonts(pdf)
        pdf.police = font
        pdf.pied_texte = "Talkpool Haiti S.A. — Warehouse Management System"
        pdf.pied_page_libelle = t("Page {n}/{total}")
        pdf.set_auto_page_break(auto=True, margin=25)

        T = lambda texte: safe_text(texte, font)
        BLEU = (43, 87, 151)
        GRIS_CLAIR = (245, 245, 245)
        GRIS_MOYEN = (120, 120, 120)
        BLANC = (255, 255, 255)
        VERT = (34, 197, 94)
        ROUGE = (239, 68, 68)
        ORANGE = (245, 158, 11)
        page_w = 170

        def _section(titre_sec):
            pdf.ln(6)
            pdf.set_fill_color(*BLEU)
            pdf.set_text_color(*BLANC)
            pdf.set_font(font, "B", 12)
            pdf.cell(0, 8, f"  {T(titre_sec)}", fill=True,
                     new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)
            pdf.ln(3)

        def _kpi_row(kpis):
            card_w = page_w / len(kpis)
            y_start = pdf.get_y()
            for i, (label, valeur, couleur) in enumerate(kpis):
                x = 20 + i * card_w
                pdf.set_xy(x, y_start)
                pdf.set_fill_color(*couleur)
                pdf.rect(x, y_start, card_w - 2, 1.5, "F")
                pdf.set_fill_color(*GRIS_CLAIR)
                pdf.rect(x, y_start + 1.5, card_w - 2, 16, "F")
                pdf.set_xy(x + 3, y_start + 3)
                pdf.set_font(font, "B", 16)
                pdf.set_text_color(*couleur)
                pdf.cell(card_w - 6, 6, str(valeur))
                pdf.set_xy(x + 3, y_start + 10)
                pdf.set_font(font, "", 8)
                pdf.set_text_color(*GRIS_MOYEN)
                pdf.cell(card_w - 6, 5, T(label))
            pdf.set_text_color(0, 0, 0)
            pdf.set_y(y_start + 20)

        def _table(headers, widths, rows, aligns=None):
            if not aligns:
                aligns = ["L"] * len(headers)
            pdf.set_fill_color(*BLEU)
            pdf.set_text_color(*BLANC)
            pdf.set_font(font, "B", 8)
            for i, h in enumerate(headers):
                pdf.cell(widths[i], 6, T(h), border=0, fill=True, align="C")
            pdf.ln()
            pdf.set_text_color(0, 0, 0)
            pdf.set_font(font, "", 8)
            for r_idx, row in enumerate(rows):
                if r_idx % 2 == 0:
                    pdf.set_fill_color(*GRIS_CLAIR)
                else:
                    pdf.set_fill_color(*BLANC)
                for i, val in enumerate(row):
                    pdf.cell(widths[i], 5.5, T(str(val)[:50]), border=0,
                             fill=True, align=aligns[i])
                pdf.ln()

        # ── Page de titre ──
        pdf.add_page()
        pdf.ln(30)
        pdf.set_fill_color(*BLEU)
        pdf.rect(20, 50, page_w, 2, "F")
        pdf.set_y(58)
        pdf.set_font(font, "B", 26)
        pdf.set_text_color(*BLEU)
        pdf.cell(0, 14, T("TALKPOOL HAITI S.A."), new_x="LMARGIN", new_y="NEXT")
        pdf.set_font(font, "B", 16)
        pdf.set_text_color(60, 60, 60)
        pdf.cell(0, 10, T(t("Rapport d'activité — Entrepôt")),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
        pdf.set_font(font, "", 12)
        pdf.set_text_color(*GRIS_MOYEN)
        periode = t("Période : {d1} — {d2}").format(
            d1=debut.strftime("%d/%m/%Y"), d2=today.strftime("%d/%m/%Y"))
        pdf.cell(0, 7, T(periode), new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 7, T(t("Date de génération : {d}").format(
            d=today.strftime("%d/%m/%Y"))),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
        pdf.rect(20, pdf.get_y(), page_w, 0.5, "F")
        pdf.ln(20)

        pdf.set_font(font, "", 10)
        pdf.set_text_color(80, 80, 80)
        pdf.cell(0, 6, T(t("Ce document présente les indicateurs clés de performance")),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 6, T(t("du système de gestion d'entrepôt (WMS) sur les 30 derniers jours.")),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.cell(0, 6, T(t("Il est destiné à la revue opérationnelle et à la prise de décision.")),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

        # ── Page KPIs ──
        pdf.add_page()

        _section(t("INDICATEURS CLÉS"))

        nb_alertes = int(resume.get("en_rupture", 0) or 0) + int(resume.get("sous_seuil", 0) or 0)
        _kpi_row([
            (t("Unités en stock"), _fmt(resume["unites_totales"]), BLEU),
            (t("Alertes stock"), str(nb_alertes), ROUGE if nb_alertes else VERT),
            (t("Couverture < 7j"), str(data.get("nb_couverture_critique", 0)), ORANGE),
            (t("Activité du jour"), str(jour.get("bons_jour", 0)), BLEU),
        ])

        pdf.ln(2)
        _kpi_row([
            (t("Reçu (30 j)"), _fmt(mouvements.get("recu_30j", 0)), VERT),
            (t("Livré (30 j)"), _fmt(mouvements.get("livre_30j", 0)), ORANGE),
            (t("Retours (30 j)"), _fmt(mouvements.get("retours_30j", 0)), BLEU),
            (t("Bons (30 j)"), _fmt(mouvements.get("bons_30j", 0)), BLEU),
        ])

        pdf.ln(2)
        pdf.set_font(font, "", 9)
        pdf.set_text_color(*GRIS_MOYEN)
        prev = data.get("mouvements_prev", {})
        for label, cle_c, cle_p in [
            (t("Réceptions"), "recu_30j", "recu_prev"),
            (t("Livraisons"), "livre_30j", "livre_prev"),
            (t("Retours"), "retours_30j", "retours_prev"),
        ]:
            cur = mouvements.get(cle_c, 0)
            prv = prev.get(cle_p, 0)
            if prv > 0:
                pct = ((cur - prv) / prv) * 100
                tendance = f"{pct:+.0f}%"
            elif cur > 0:
                tendance = "+100%"
            else:
                tendance = "0%"
            pdf.cell(56, 5, T(f"  {label} : {tendance} vs période précédente"))
        pdf.ln()
        pdf.set_text_color(0, 0, 0)

        if self._is_fuel:
            # Une seule cuve, pas de région : ce qui compte, c'est QUI a
            # consommé le carburant — véhicule, projet, receveur.
            top_vehicules = data.get("top_vehicules", [])
            par_projet = data.get("par_projet", [])
            par_receveur = data.get("par_receveur", [])

            if top_vehicules:
                _section(t("TOP VÉHICULES CONSOMMATEURS (30 J)"))
                _table(
                    ["#", t("Plaque"), t("Modèle"), t("Quantité (gal)")],
                    [8, 45, 60, 57],
                    [(str(i + 1), v["plaque"], (v.get("modele") or "")[:30],
                      _fmt(v["quantite"]))
                     for i, v in enumerate(top_vehicules)],
                    ["C", "L", "L", "R"],
                )

            if par_projet:
                _section(t("CONSOMMATION PAR PROJET (30 J)"))
                total_p = sum(p["quantite"] for p in par_projet) or 1
                _table(
                    [t("Projet"), t("Quantité (gal)"), t("Part")],
                    [80, 45, 45],
                    [(p["projet"], _fmt(p["quantite"]),
                      f"{p['quantite'] / total_p * 100:.1f}%")
                     for p in par_projet],
                    ["L", "R", "R"],
                )

            if par_receveur:
                _section(t("CONSOMMATION PAR RECEVEUR (30 J)"))
                total_rc = sum(r["quantite"] for r in par_receveur) or 1
                _table(
                    [t("Receveur"), t("Quantité (gal)"), t("Part")],
                    [80, 45, 45],
                    [(r["receveur"], _fmt(r["quantite"]),
                      f"{r['quantite'] / total_rc * 100:.1f}%")
                     for r in par_receveur],
                    ["L", "R", "R"],
                )
        else:
            # ── Top sorties ──
            _section(t("TOP 10 DES PRODUITS LES PLUS SORTIS"))
            nb_top = min(len(top), 10)
            _table(
                ["#", t("Produit"), "SKU", t("Catégorie"), t("Quantité")],
                [8, 70, 30, 35, 27],
                [(str(i + 1), (l.get("name") or l["sku"])[:35], l["sku"],
                  l.get("category", "")[:20], _fmt(l["quantite"]))
                 for i, l in enumerate(top[:nb_top])],
                ["C", "L", "L", "L", "R"],
            )

            # ── Stock par catégorie ──
            if categories:
                _section(t("RÉPARTITION DU STOCK PAR CATÉGORIE"))
                total_unites = sum(c["unites"] for c in categories) or 1
                _table(
                    [t("Catégorie"), t("Unités"), t("Part")],
                    [80, 45, 45],
                    [(c["categorie"], _fmt(c["unites"]),
                      f"{c['unites'] / total_unites * 100:.1f}%")
                     for c in categories],
                    ["L", "R", "R"],
                )

            # ── Livraisons par région ──
            if par_region:
                _section(t("LIVRAISONS PAR RÉGION (30 J)"))
                total_reg = sum(r["quantite"] for r in par_region) or 1
                _table(
                    [t("Région"), t("Quantité"), t("Part")],
                    [80, 45, 45],
                    [(r["region"], _fmt(r["quantite"]),
                      f"{r['quantite'] / total_reg * 100:.1f}%")
                     for r in par_region],
                    ["L", "R", "R"],
                )

        # ── Alertes ──
        total_alertes = max(nb_alertes, len(alertes))
        nb_ruptures = int(resume.get("en_rupture", 0) or 0)
        _section(t("ALERTES STOCK ({n} produit(s), {r} en rupture)").format(
            n=total_alertes, r=nb_ruptures))

        if alertes:
            rows_al = []
            for a in alertes[:40]:
                stock = a["current_stock"]
                seuil = a["min_stock"]
                if stock <= 0:
                    etat = t("RUPTURE")
                elif seuil > 0 and stock <= seuil / 2:
                    etat = t("CRITIQUE")
                else:
                    etat = t("BAS")
                rows_al.append((a["sku"], (a.get("name") or "")[:30],
                                _fmt(stock), _fmt(seuil), etat))
            _table(
                ["SKU", t("Produit"), t("Stock"), t("Seuil"), t("État")],
                [30, 65, 25, 25, 25],
                rows_al,
                ["L", "L", "R", "R", "C"],
            )
            if total_alertes > len(rows_al):
                pdf.set_font(font, "", 8)
                pdf.set_text_color(*GRIS_MOYEN)
                pdf.cell(0, 5, T(t("{n} premiers sur {total}").format(
                    n=len(rows_al), total=total_alertes)),
                         new_x="LMARGIN", new_y="NEXT")
                pdf.set_text_color(0, 0, 0)
        else:
            pdf.set_font(font, "", 10)
            pdf.set_text_color(*VERT)
            pdf.cell(0, 6, T(t("Aucune alerte — tous les stocks sont conformes.")),
                     new_x="LMARGIN", new_y="NEXT")
            pdf.set_text_color(0, 0, 0)

        # ── Couverture ──
        if couverture:
            _section(t("COUVERTURE DE STOCK (PRODUITS < 14 JOURS)"))
            _table(
                ["SKU", t("Produit"), t("Stock"), t("Conso/jour"), t("Jours restants")],
                [30, 60, 25, 28, 27],
                [(c["sku"], c["name"][:30], _fmt(c["current_stock"]),
                  f"{c['conso_jour']:.1f}", f"{c['jours_restants']:.0f}")
                 for c in couverture[:30]],
                ["L", "L", "R", "R", "R"],
            )

        # ── Signature ──
        pdf.ln(16)
        y_sig = pdf.get_y()
        if y_sig > 245:
            pdf.add_page()
            y_sig = pdf.get_y() + 10
        pdf.set_draw_color(*BLEU)
        pdf.set_font(font, "B", 10)
        pdf.set_text_color(*BLEU)
        pdf.cell(0, 6, T(t("APPROBATION")), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(4)
        y_sig = pdf.get_y()
        pdf.set_text_color(0, 0, 0)
        pdf.set_font(font, "", 9)
        pdf.set_draw_color(*GRIS_MOYEN)
        for col_x, label in [(20, t("Préparé par")), (75, t("Approuvé par")),
                              (130, t("Date"))]:
            pdf.line(col_x, y_sig + 12, col_x + 50, y_sig + 12)
            pdf.set_xy(col_x, y_sig + 14)
            pdf.cell(50, 5, T(label), align="C")

        # ── Confidentialité ──
        pdf.ln(16)
        pdf.set_font(font, "", 7)
        pdf.set_text_color(*GRIS_MOYEN)
        pdf.cell(0, 4, T(t("CONFIDENTIEL — Ce document est la propriété de "
                            "Talkpool Haiti S.A. et ne doit pas être distribué "
                            "sans autorisation.")),
                 new_x="LMARGIN", new_y="NEXT")
        pdf.set_text_color(0, 0, 0)

        try:
            pdf.output(path)
        except (OSError, PermissionError) as e:
            messagebox.showerror(
                t("Rapport"),
                t("Impossible d'écrire le fichier :\n{e}\n\nVérifie qu'il "
                  "n'est pas déjà ouvert dans un autre programme.").format(e=e))
            return
        messagebox.showinfo(t("Rapport"),
                            t("Rapport exporté vers {path}").format(path=path))
        webbrowser.open(f"file:///{path.replace(os.sep, '/')}")

    def _export_excel(self):
        if not self._last_data:
            messagebox.showinfo(t("Export"),
                                t("Données non disponibles. Attends le chargement."))
            return

        path = filedialog.asksaveasfilename(
            defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")],
            initialfile=f"Talkpool_Dashboard_{date.today().strftime('%Y-%m-%d')}.xlsx")
        if not path:
            return

        from openpyxl import Workbook
        from openpyxl.chart import BarChart as XlBarChart, PieChart, Reference
        from openpyxl.styles import (Font, PatternFill, Alignment, Border, Side,
                                     NamedStyle, numbers)
        from openpyxl.utils import get_column_letter

        data = self._last_data
        resume = data["resume"]
        mouvements = data["mouvements"]
        jour = data.get("activite_jour", {})
        today = date.today()
        debut = today - timedelta(days=30)

        wb = Workbook()

        # ── Styles ──
        BLEU = "2B5797"
        BLEU_CLAIR = "D6E4F0"
        GRIS = "F2F2F2"
        ROUGE_FILL = "FDE8E8"
        ORANGE_FILL = "FEF3CD"

        titre_font = Font(name="Calibri", size=18, bold=True, color=BLEU)
        sous_titre_font = Font(name="Calibri", size=11, color="666666")
        section_font = Font(name="Calibri", size=13, bold=True, color="FFFFFF")
        section_fill = PatternFill(start_color=BLEU, end_color=BLEU,
                                    fill_type="solid")
        en_tete_font = Font(name="Calibri", size=10, bold=True, color="FFFFFF")
        en_tete_fill = PatternFill(start_color=BLEU, end_color=BLEU,
                                    fill_type="solid")
        kpi_val_font = Font(name="Calibri", size=20, bold=True, color=BLEU)
        kpi_label_font = Font(name="Calibri", size=9, color="666666")
        kpi_fill = PatternFill(start_color=BLEU_CLAIR, end_color=BLEU_CLAIR,
                                fill_type="solid")
        data_font = Font(name="Calibri", size=10)
        data_font_bold = Font(name="Calibri", size=10, bold=True)
        rouge_font = Font(name="Calibri", size=10, bold=True, color="CC0000")
        orange_font = Font(name="Calibri", size=10, bold=True, color="CC6600")
        alt_fill = PatternFill(start_color=GRIS, end_color=GRIS,
                                fill_type="solid")
        rupture_fill = PatternFill(start_color=ROUGE_FILL, end_color=ROUGE_FILL,
                                    fill_type="solid")
        critique_fill = PatternFill(start_color=ORANGE_FILL,
                                     end_color=ORANGE_FILL, fill_type="solid")
        bordure = Border(
            bottom=Side(style="thin", color="D0D0D0"))
        bordure_pleine = Border(
            left=Side(style="thin", color="C0C0C0"),
            right=Side(style="thin", color="C0C0C0"),
            top=Side(style="thin", color="C0C0C0"),
            bottom=Side(style="thin", color="C0C0C0"))
        center = Alignment(horizontal="center", vertical="center")
        right_al = Alignment(horizontal="right", vertical="center")
        left_al = Alignment(horizontal="left", vertical="center")

        def _header_row(ws, row, cols):
            for i, (txt, width) in enumerate(cols, 1):
                c = ws.cell(row=row, column=i, value=txt)
                c.font = en_tete_font
                c.fill = en_tete_fill
                c.alignment = center
                c.border = bordure_pleine
                ws.column_dimensions[get_column_letter(i)].width = width
            ws.row_dimensions[row].height = 22

        def _data_row(ws, row, vals, aligns=None, style_fn=None):
            fill = alt_fill if row % 2 == 0 else PatternFill()
            if style_fn:
                fill = style_fn(vals) or fill
            for i, val in enumerate(vals, 1):
                c = ws.cell(row=row, column=i, value=val)
                c.font = data_font
                c.border = bordure
                c.fill = fill
                al = (aligns or {}).get(i, left_al)
                c.alignment = al

        # ═══════════ Feuille DASHBOARD ═══════════
        ws = wb.active
        ws.title = "Dashboard"
        ws.sheet_properties.tabColor = BLEU

        ws.merge_cells("A1:H1")
        ws["A1"] = "TALKPOOL HAITI S.A."
        ws["A1"].font = titre_font
        ws.row_dimensions[1].height = 32

        ws.merge_cells("A2:H2")
        ws["A2"] = t("Rapport d'activité — {d1} au {d2}").format(
            d1=debut.strftime("%d/%m/%Y"), d2=today.strftime("%d/%m/%Y"))
        ws["A2"].font = sous_titre_font
        ws.row_dimensions[2].height = 18

        ws.merge_cells("A4:H4")
        ws["A4"] = t("  INDICATEURS CLÉS")
        ws["A4"].font = section_font
        ws["A4"].fill = section_fill
        ws.row_dimensions[4].height = 26

        kpis = [
            (t("Unités en stock"), resume["unites_totales"]),
            (t("Valeur du stock"), resume.get("valeur_totale", 0)),
            (t("Références"), resume.get("references_total", 0)),
            (t("Alertes stock"), int(resume.get("en_rupture", 0) or 0)
             + int(resume.get("sous_seuil", 0) or 0)),
        ]
        for i, (label, val) in enumerate(kpis):
            col = i * 2 + 1
            ws.merge_cells(start_row=6, start_column=col,
                           end_row=6, end_column=col + 1)
            c = ws.cell(row=6, column=col, value=val)
            c.font = kpi_val_font
            c.fill = kpi_fill
            c.alignment = center
            c_r = ws.cell(row=6, column=col + 1)
            c_r.fill = kpi_fill
            ws.merge_cells(start_row=7, start_column=col,
                           end_row=7, end_column=col + 1)
            c2 = ws.cell(row=7, column=col, value=label)
            c2.font = kpi_label_font
            c2.alignment = center
        ws.row_dimensions[6].height = 36
        ws.row_dimensions[7].height = 18

        kpis2 = [
            (t("Reçu (30 j)"), mouvements.get("recu_30j", 0)),
            (t("Livré (30 j)"), mouvements.get("livre_30j", 0)),
            (t("Retours (30 j)"), mouvements.get("retours_30j", 0)),
            (t("Bons (30 j)"), mouvements.get("bons_30j", 0)),
        ]
        for i, (label, val) in enumerate(kpis2):
            col = i * 2 + 1
            ws.merge_cells(start_row=9, start_column=col,
                           end_row=9, end_column=col + 1)
            c = ws.cell(row=9, column=col, value=val)
            c.font = Font(name="Calibri", size=16, bold=True, color="2E7D32")
            c.fill = kpi_fill
            c.alignment = center
            c_r = ws.cell(row=9, column=col + 1)
            c_r.fill = kpi_fill
            ws.merge_cells(start_row=10, start_column=col,
                           end_row=10, end_column=col + 1)
            c2 = ws.cell(row=10, column=col, value=label)
            c2.font = kpi_label_font
            c2.alignment = center
        ws.row_dimensions[9].height = 32

        for i in range(1, 9):
            ws.column_dimensions[get_column_letter(i)].width = 16

        if self._is_fuel:
            # ═══════════ Feuille TOP VÉHICULES ═══════════
            ws2 = wb.create_sheet(t("Top véhicules"))
            ws2.sheet_properties.tabColor = "E67E22"
            _header_row(ws2, 1, [("#", 6), (t("Plaque"), 20), (t("Modèle"), 30),
                                  (t("Quantité (gal)"), 18)])
            top_vehicules = data.get("top_vehicules", [])
            for r, v in enumerate(top_vehicules, 2):
                _data_row(ws2, r, [r - 1, v["plaque"], v.get("modele") or "",
                                    v["quantite"]],
                          aligns={1: center, 4: right_al})

            if top_vehicules:
                chart = XlBarChart()
                chart.type = "bar"
                chart.style = 10
                chart.title = t("Top véhicules consommateurs (30 j)")
                chart.y_axis.title = t("Quantité (gal)")
                chart.width = 28
                chart.height = 14
                cat_ref = Reference(ws2, min_col=2, min_row=2,
                                    max_row=1 + len(top_vehicules))
                val_ref = Reference(ws2, min_col=4, min_row=1,
                                    max_row=1 + len(top_vehicules))
                chart.add_data(val_ref, titles_from_data=True)
                chart.set_categories(cat_ref)
                chart.shape = 4
                ws2.add_chart(chart, "A" + str(len(top_vehicules) + 4))

            # ═══════════ Feuille PAR PROJET ═══════════
            ws3 = wb.create_sheet(t("Par projet"))
            ws3.sheet_properties.tabColor = "3498DB"
            _header_row(ws3, 1, [(t("Projet"), 32), (t("Quantité (gal)"), 18),
                                  (t("Part (%)"), 14)])
            par_projet = data.get("par_projet", [])
            total_p = sum(p["quantite"] for p in par_projet) or 1
            for r, p in enumerate(par_projet, 2):
                pct = p["quantite"] / total_p * 100
                _data_row(ws3, r, [p["projet"], p["quantite"], round(pct, 1)],
                          aligns={2: right_al, 3: right_al})
                ws3.cell(row=r, column=3).number_format = "0.0"

            if par_projet:
                pie = PieChart()
                pie.title = t("Consommation par projet")
                pie.width = 22
                pie.height = 14
                pie.style = 10
                cats_ref = Reference(ws3, min_col=1, min_row=2,
                                      max_row=1 + len(par_projet))
                vals_ref = Reference(ws3, min_col=2, min_row=1,
                                      max_row=1 + len(par_projet))
                pie.add_data(vals_ref, titles_from_data=True)
                pie.set_categories(cats_ref)
                ws3.add_chart(pie, "A" + str(len(par_projet) + 4))

            # ═══════════ Feuille PAR RECEVEUR ═══════════
            ws4 = wb.create_sheet(t("Par receveur"))
            ws4.sheet_properties.tabColor = "27AE60"
            _header_row(ws4, 1, [(t("Receveur"), 28), (t("Quantité (gal)"), 18),
                                  (t("Part (%)"), 14)])
            par_receveur = data.get("par_receveur", [])
            total_rc = sum(r["quantite"] for r in par_receveur) or 1
            for r, rc in enumerate(par_receveur, 2):
                pct = rc["quantite"] / total_rc * 100
                _data_row(ws4, r, [rc["receveur"], rc["quantite"],
                                    round(pct, 1)],
                          aligns={2: right_al, 3: right_al})

            if par_receveur:
                chart_r = XlBarChart()
                chart_r.type = "col"
                chart_r.style = 10
                chart_r.title = t("Consommation par receveur (30 j)")
                chart_r.width = 24
                chart_r.height = 14
                cat_ref = Reference(ws4, min_col=1, min_row=2,
                                    max_row=1 + len(par_receveur))
                val_ref = Reference(ws4, min_col=2, min_row=1,
                                    max_row=1 + len(par_receveur))
                chart_r.add_data(val_ref, titles_from_data=True)
                chart_r.set_categories(cat_ref)
                ws4.add_chart(chart_r, "A" + str(len(par_receveur) + 4))
        else:
            # ═══════════ Feuille TOP SORTIES ═══════════
            ws2 = wb.create_sheet(t("Top sorties"))
            ws2.sheet_properties.tabColor = "E67E22"
            _header_row(ws2, 1, [("#", 6), ("SKU", 16), (t("Produit"), 38),
                                  (t("Catégorie"), 22), (t("Quantité"), 16)])
            top_data = data["top_sorties"][:10]
            for r, ligne in enumerate(top_data, 2):
                _data_row(ws2, r, [r - 1, ligne["sku"], ligne.get("name", ""),
                                    ligne.get("category", ""), ligne["quantite"]],
                          aligns={1: center, 5: right_al})

            if top_data:
                chart = XlBarChart()
                chart.type = "bar"
                chart.style = 10
                chart.title = t("Top 10 des sorties (30 j)")
                chart.x_axis.title = None
                chart.y_axis.title = t("Quantité")
                chart.width = 28
                chart.height = 14
                cat_ref = Reference(ws2, min_col=3, min_row=2,
                                    max_row=1 + len(top_data))
                val_ref = Reference(ws2, min_col=5, min_row=1,
                                    max_row=1 + len(top_data))
                chart.add_data(val_ref, titles_from_data=True)
                chart.set_categories(cat_ref)
                chart.shape = 4
                ws2.add_chart(chart, "A" + str(len(top_data) + 4))

            # ═══════════ Feuille STOCK PAR CATÉGORIE ═══════════
            ws3 = wb.create_sheet(t("Stock par catégorie"))
            ws3.sheet_properties.tabColor = "3498DB"
            _header_row(ws3, 1, [(t("Catégorie"), 32), (t("Unités"), 16),
                                  (t("Part (%)"), 14)])
            total_u = sum(c["unites"] for c in data["categories"]) or 1
            for r, cat in enumerate(data["categories"], 2):
                pct = cat["unites"] / total_u * 100
                _data_row(ws3, r, [cat["categorie"], cat["unites"],
                                    round(pct, 1)],
                          aligns={2: right_al, 3: right_al})
                ws3.cell(row=r, column=3).number_format = "0.0"

            if data["categories"]:
                pie = PieChart()
                pie.title = t("Répartition du stock")
                pie.width = 22
                pie.height = 14
                pie.style = 10
                cats_ref = Reference(ws3, min_col=1, min_row=2,
                                      max_row=1 + len(data["categories"]))
                vals_ref = Reference(ws3, min_col=2, min_row=1,
                                      max_row=1 + len(data["categories"]))
                pie.add_data(vals_ref, titles_from_data=True)
                pie.set_categories(cats_ref)
                ws3.add_chart(pie, "A" + str(len(data["categories"]) + 4))

            # ═══════════ Feuille PAR RÉGION ═══════════
            ws4 = wb.create_sheet(t("Par région"))
            ws4.sheet_properties.tabColor = "27AE60"
            _header_row(ws4, 1, [(t("Région"), 28), (t("Quantité livrée"), 18),
                                  (t("Part (%)"), 14)])
            total_r = sum(r["quantite"] for r in data["par_region"]) or 1
            for r, reg in enumerate(data["par_region"], 2):
                pct = reg["quantite"] / total_r * 100
                _data_row(ws4, r, [reg["region"], reg["quantite"],
                                    round(pct, 1)],
                          aligns={2: right_al, 3: right_al})

            if data["par_region"]:
                chart_r = XlBarChart()
                chart_r.type = "col"
                chart_r.style = 10
                chart_r.title = t("Livraisons par région (30 j)")
                chart_r.width = 24
                chart_r.height = 14
                cat_ref = Reference(ws4, min_col=1, min_row=2,
                                    max_row=1 + len(data["par_region"]))
                val_ref = Reference(ws4, min_col=2, min_row=1,
                                    max_row=1 + len(data["par_region"]))
                chart_r.add_data(val_ref, titles_from_data=True)
                chart_r.set_categories(cat_ref)
                ws4.add_chart(chart_r, "A" + str(len(data["par_region"]) + 4))

        # ═══════════ Feuille ALERTES ═══════════
        ws5 = wb.create_sheet(t("Alertes"))
        ws5.sheet_properties.tabColor = "E74C3C"
        _header_row(ws5, 1, [("SKU", 16), (t("Produit"), 38), (t("Stock"), 12),
                              (t("Seuil"), 12), (t("État"), 14),
                              (t("Écart"), 12)])
        for r, a in enumerate(data["alertes"], 2):
            stock = a["current_stock"]
            seuil = a["min_stock"]
            ecart = stock - seuil
            if stock <= 0:
                etat = t("RUPTURE")
                sf = lambda v: rupture_fill
            elif seuil > 0 and stock <= seuil / 2:
                etat = t("CRITIQUE")
                sf = lambda v: critique_fill
            else:
                etat = t("BAS")
                sf = None
            _data_row(ws5, r, [a["sku"], a.get("name", ""), stock, seuil,
                                etat, ecart],
                      aligns={3: right_al, 4: right_al, 5: center, 6: right_al},
                      style_fn=sf)
            if stock <= 0:
                for col in range(1, 7):
                    ws5.cell(row=r, column=col).font = rouge_font
            elif seuil > 0 and stock <= seuil / 2:
                for col in range(1, 7):
                    ws5.cell(row=r, column=col).font = orange_font

        # ═══════════ Feuille COUVERTURE ═══════════
        couverture = data.get("couverture", [])
        if couverture:
            ws6 = wb.create_sheet(t("Couverture"))
            ws6.sheet_properties.tabColor = "F39C12"
            _header_row(ws6, 1, [("SKU", 16), (t("Produit"), 38),
                                  (t("Stock"), 14), (t("Conso/jour"), 14),
                                  (t("Jours restants"), 16)])
            for r, c in enumerate(couverture, 2):
                jours = round(c["jours_restants"])
                if jours < 7:
                    sf = lambda v: rupture_fill
                else:
                    sf = None
                _data_row(ws6, r, [c["sku"], c["name"], c["current_stock"],
                                    round(c["conso_jour"], 1), jours],
                          aligns={3: right_al, 4: right_al, 5: right_al},
                          style_fn=sf)
                if jours < 7:
                    ws6.cell(row=r, column=5).font = rouge_font

        # ═══════════ Impressions ═══════════
        for sheet in wb.sheetnames:
            s = wb[sheet]
            s.sheet_view.showGridLines = False
            s.print_options.horizontalCentered = True

        try:
            wb.save(path)
        except (OSError, PermissionError) as e:
            messagebox.showerror(
                t("Export"),
                t("Impossible d'écrire le fichier :\n{e}\n\nVérifie qu'il "
                  "n'est pas déjà ouvert dans un autre programme.").format(e=e))
            return
        messagebox.showinfo(t("Export"),
                            t("Dashboard exporté vers {path}").format(path=path))
        webbrowser.open(f"file:///{path.replace(os.sep, '/')}")

    # -------------------------------------------------------- clics cartes KPI

    def _on_clic_documents(self, doc_type: str):
        titres = {
            "RECEIVING": t("Dernières réceptions (30 j)"),
            "DELIVERY": t("Dernières livraisons (30 j)"),
            "RETURN": t("Derniers retours (30 j)"),
        }
        titre = titres.get(doc_type, doc_type)
        date_from = (date.today() - timedelta(days=30)).isoformat()
        run_async(
            self,
            lambda: self.api.list_documents(doc_type=doc_type, limit=15,
                                            date_from=date_from),
            lambda result: DocumentsPopup(self, titre, result[0]),
            lambda exc: None,
        )

    def _on_clic_documents_jour(self):
        date_from = date.today().isoformat()
        run_async(
            self,
            lambda: self.api.list_documents(limit=20, date_from=date_from),
            lambda result: DocumentsPopup(self, t("Activité du jour"), result[0]),
            lambda exc: None,
        )

    def _on_clic_alertes(self):
        if self._alertes_data:
            AlertesPopup(self, self._alertes_data, self._alertes_total)

    def _on_clic_couverture(self):
        self.tree_couverture.focus_set()
        children = self.tree_couverture.get_children()
        if children:
            self.tree_couverture.see(children[0])
            self.tree_couverture.selection_set(children[0])

    def _on_error(self, exc):
        self._backoff = min(self._backoff * 2, BACKOFF_MAX_MS)
        self.status_label.configure(
            text=t("●  Hors ligne — tentative dans {s}s").format(
                s=self._backoff // 1000),
            text_color="#f59e0b",
        )
        self._replanifier()

    def _rafraichir_bientot(self):
        """Avance le prochain rafraîchissement suite à un événement WebSocket.

        Le tableau de bord est déjà rafraîchi en boucle : on se contente de
        replanifier ce cycle un peu plus tôt, sans lancer de requête
        supplémentaire en parallèle. Un bon de 10 lignes génère 10 messages
        `stock_update` d'affilée : ce court délai les regroupe en un seul
        rafraîchissement.
        """
        if not self.winfo_exists():
            return
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
        self._job = self.after(400, self._poll)

    def maj_stock_produit(self, product_id: int, new_stock: float):
        """Appelée lorsqu'une mise à jour de stock arrive via WebSocket."""
        self._rafraichir_bientot()

    def notifier_nouveau_document(self, data: dict):
        """Appelée lorsqu'un document_created arrive via WebSocket."""
        self._rafraichir_bientot()

    def _replanifier(self):
        if self.winfo_exists():
            delai = self._backoff
            ws = getattr(self.api, "websocket_client", None)
            if ws is not None and ws.connected:
                delai = max(delai, REFRESH_MS_WS_ACTIF)
            self._job = self.after(delai, self._poll)

    def destroy(self):
        if self._job is not None:
            try:
                self.after_cancel(self._job)
            except Exception:
                pass
            self._job = None
        super().destroy()


class EcartsDialog(ctk.CTkToplevel):
    """Détail des écarts de réconciliation : quel produit, quel écart.

    Sans cette fenêtre, la seule façon de savoir de quoi parlait l'alerte
    du tableau de bord était d'interroger le serveur à la main.
    """

    def __init__(self, master, data: dict):
        super().__init__(master)
        self.title(t("Écarts de stock détectés"))
        self.geometry("760x480")

        self.update_idletasks()
        x = master.winfo_rootx() + 60
        y = master.winfo_rooty() + 40
        self.geometry(f"760x480+{x}+{y}")

        self.transient(master)
        self.lift()
        self.grab_set()

        ecarts = data.get("ecarts") or []
        ecarts_emplacement = data.get("ecarts_emplacement") or []

        conteneur = ctk.CTkScrollableFrame(self, fg_color="transparent")
        conteneur.pack(fill="both", expand=True, padx=16, pady=16)

        if not ecarts and not ecarts_emplacement:
            ctk.CTkLabel(conteneur,
                         text=t("Aucun écart — le grand livre est cohérent."),
                         text_color="#22c55e").pack(anchor="w", pady=20)
            return

        if ecarts:
            ctk.CTkLabel(
                conteneur,
                text=t("Stock enregistré ≠ stock recalculé depuis les mouvements"),
                font=ctk.CTkFont(size=13, weight="bold"), text_color="#f0f0f0",
            ).pack(anchor="w", pady=(0, 4))
            self._table(
                conteneur,
                columns=("sku", "name", "current_stock", "stock_calcule", "difference"),
                headings={"sku": "SKU", "name": t("Produit"),
                          "current_stock": t("Stock enregistré"),
                          "stock_calcule": t("Stock recalculé"),
                          "difference": t("Écart")},
                widths={"sku": 110, "name": 280, "current_stock": 120,
                        "stock_calcule": 120, "difference": 90},
                rows=[(e["sku"], e["name"], f"{e['current_stock']:g}",
                       f"{e['stock_calcule']:g}", f"{e['difference']:+g}") for e in ecarts],
            )

        if ecarts_emplacement:
            ctk.CTkLabel(
                conteneur,
                text=t("Stock entrepôt (par emplacement) ≠ stock produit"),
                font=ctk.CTkFont(size=13, weight="bold"), text_color="#f0f0f0",
            ).pack(anchor="w", pady=(16, 4))
            self._table(
                conteneur,
                columns=("sku", "name", "current_stock", "stock_entrepot", "difference"),
                headings={"sku": "SKU", "name": t("Produit"),
                          "current_stock": t("Stock produit"),
                          "stock_entrepot": t("Stock entrepôt (calculé)"),
                          "difference": t("Écart")},
                widths={"sku": 110, "name": 280, "current_stock": 120,
                        "stock_entrepot": 150, "difference": 90},
                rows=[(e["sku"], e["name"], f"{e['current_stock']:g}",
                       f"{e['stock_entrepot']:g}",
                       f"{e['current_stock'] - e['stock_entrepot']:+g}")
                      for e in ecarts_emplacement],
            )

    def _table(self, parent, columns, headings, widths, rows):
        cadre = ctk.CTkFrame(parent, fg_color="#1a1d23", corner_radius=8,
                             border_width=1, border_color="#2a2d35")
        cadre.pack(fill="x", pady=(0, 4))
        tree = ttk.Treeview(cadre, columns=columns, show="headings",
                            height=min(max(len(rows), 1), 8))
        for col in columns:
            tree.heading(col, text=headings[col],
                        command=lambda c=col, t=tree: _sort_treeview(t, c))
            tree.column(col, width=widths[col], anchor="w")
        for row in rows:
            tree.insert("", "end", values=row)
        tree.pack(fill="x", padx=4, pady=4)


class DocumentsPopup(ctk.CTkToplevel):
    """Popup des derniers bons avec détail produits au clic."""

    TYPES_FR = {
        "RECEIVING": t("Réception"),
        "DELIVERY": t("Livraison"),
        "RETURN": t("Retour"),
        "SUPPLIER_RETURN": t("Retour fournisseur"),
        "ADJUSTMENT": t("Ajustement"),
    }

    def __init__(self, master, titre: str, documents: list[dict]):
        super().__init__(master)
        self.title(titre)
        self.geometry("960x580")
        self.update_idletasks()
        x = master.winfo_rootx() + 30
        y = master.winfo_rooty() + 20
        self.geometry(f"960x580+{x}+{y}")
        self.transient(master)
        self.lift()
        self.grab_set()
        self._documents = documents

        ctk.CTkLabel(self, text=titre,
                     font=ctk.CTkFont(size=16, weight="bold"),
                     text_color="#f0f0f0").pack(anchor="w", padx=16, pady=(16, 4))

        if not documents:
            ctk.CTkLabel(self, text=t("Aucun mouvement sur cette période."),
                         text_color="#9ca3af").pack(anchor="w", padx=16, pady=20)
            return

        ctk.CTkLabel(self, text=t("Cliquer sur un bon pour voir les produits"),
                     text_color="#6b7280",
                     font=ctk.CTkFont(size=10)).pack(anchor="w", padx=16, pady=(0, 6))

        paned = ctk.CTkFrame(self, fg_color="transparent")
        paned.pack(fill="both", expand=True, padx=16, pady=(0, 16))
        paned.grid_columnconfigure(0, weight=3)
        paned.grid_columnconfigure(1, weight=2)
        paned.grid_rowconfigure(0, weight=1)

        cadre_gauche = ctk.CTkFrame(paned, fg_color="#1a1d23", corner_radius=8,
                                     border_width=1, border_color="#2a2d35")
        cadre_gauche.grid(row=0, column=0, sticky="nsew", padx=(0, 6))

        cols = ("reference", "date", "lignes", "operateur", "region")
        self.tree_docs = ttk.Treeview(cadre_gauche, columns=cols, show="headings",
                                       height=min(len(documents), 18))
        for col, texte, larg in [
            ("reference", t("Référence"), 140),
            ("date", t("Date"), 120),
            ("lignes", t("Lignes"), 50),
            ("operateur", t("Opérateur"), 120),
            ("region", t("Région"), 100),
        ]:
            self.tree_docs.heading(col, text=texte,
                                    command=lambda c=col: _sort_treeview(self.tree_docs, c))
            self.tree_docs.column(col, width=larg, anchor="w")

        self._doc_map = {}
        for i, d in enumerate(documents):
            lines = d.get("lines", [])
            nb = len(lines)
            date_str = (d.get("created_at") or "")[:16].replace("T", " ")
            iid = self.tree_docs.insert("", "end", values=(
                d.get("reference", ""),
                date_str,
                nb,
                d.get("created_by_name") or d.get("created_by", ""),
                d.get("region") or "—",
            ))
            self._doc_map[iid] = d

        self.tree_docs.pack(fill="both", expand=True, padx=4, pady=4)
        sb = ttk.Scrollbar(cadre_gauche, orient="vertical",
                           command=self.tree_docs.yview)
        self.tree_docs.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.tree_docs.bind("<<TreeviewSelect>>", self._on_select_doc)

        cadre_droit = ctk.CTkFrame(paned, fg_color="#1a1d23", corner_radius=8,
                                    border_width=1, border_color="#2a2d35")
        cadre_droit.grid(row=0, column=1, sticky="nsew", padx=(6, 0))

        self.detail_titre = ctk.CTkLabel(cadre_droit, text=t("Produits du bon"),
                                          font=ctk.CTkFont(size=13, weight="bold"),
                                          text_color="#f0f0f0")
        self.detail_titre.pack(anchor="w", padx=10, pady=(10, 4))

        cols_p = ("sku", "produit", "quantite")
        self.tree_produits = ttk.Treeview(cadre_droit, columns=cols_p,
                                           show="headings", height=18)
        for col, texte, larg in [
            ("sku", "SKU", 100),
            ("produit", t("Produit"), 160),
            ("quantite", t("Qté"), 60),
        ]:
            self.tree_produits.heading(col, text=texte)
            self.tree_produits.column(col, width=larg, anchor="w")
        self.tree_produits.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self.detail_total = ctk.CTkLabel(cadre_droit, text="",
                                          font=ctk.CTkFont(size=11),
                                          text_color="#9ca3af")
        self.detail_total.pack(anchor="w", padx=10, pady=(0, 8))

        if documents:
            first = self.tree_docs.get_children()[0]
            self.tree_docs.selection_set(first)
            self.tree_docs.focus(first)

    def _on_select_doc(self, _event):
        sel = self.tree_docs.selection()
        if not sel:
            return
        doc = self._doc_map.get(sel[0])
        if not doc:
            return
        ref = doc.get("reference", "")
        self.detail_titre.configure(text=f"{ref}")
        self.tree_produits.delete(*self.tree_produits.get_children())
        lines = doc.get("lines", [])
        total_qty = 0
        for li in lines:
            qty = li.get("quantity", 0)
            total_qty += qty
            self.tree_produits.insert("", "end", values=(
                li.get("sku", ""),
                li.get("name", ""),
                _fmt(qty),
            ))
        self.detail_total.configure(
            text=t("{n} produit(s) — {q} unités").format(
                n=len(lines), q=_fmt(total_qty)))


class AlertesPopup(ctk.CTkToplevel):
    """Popup détaillée des alertes stock."""

    def __init__(self, master, alertes: list[dict], total: int):
        super().__init__(master)
        self.title(t("Alertes stock"))
        self.geometry("700x480")
        self.update_idletasks()
        x = master.winfo_rootx() + 40
        y = master.winfo_rooty() + 30
        self.geometry(f"700x480+{x}+{y}")
        self.transient(master)
        self.lift()
        self.grab_set()

        entete = ctk.CTkFrame(self, fg_color="transparent")
        entete.pack(fill="x", padx=16, pady=(16, 8))
        ctk.CTkLabel(entete, text=t("Alertes stock"),
                     font=ctk.CTkFont(size=16, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")
        ctk.CTkLabel(entete,
                     text=t("{n} produit(s)").format(n=total),
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color="#ef4444").pack(side="right")

        cadre = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=8,
                             border_width=1, border_color="#2a2d35")
        cadre.pack(fill="both", expand=True, padx=16, pady=(0, 16))

        cols = ("sku", "nom", "stock", "seuil", "etat")
        tree = ttk.Treeview(cadre, columns=cols, show="headings",
                            height=min(len(alertes), 15))
        for col, texte, larg in [
            ("sku", "SKU", 110),
            ("nom", t("Produit"), 250),
            ("stock", t("Stock"), 80),
            ("seuil", t("Seuil"), 80),
            ("etat", t("État"), 100),
        ]:
            tree.heading(col, text=texte,
                         command=lambda c=col, tr=tree: _sort_treeview(tr, c))
            tree.column(col, width=larg, anchor="w")

        tree.tag_configure("rupture", foreground="#ef4444")
        tree.tag_configure("critique", foreground="#f59e0b")
        tree.tag_configure("bas", foreground="#3b82f6")

        for a in alertes:
            stock = a["current_stock"]
            seuil = a["min_stock"]
            if stock <= 0:
                etat, tag = t("RUPTURE"), "rupture"
            elif seuil > 0 and stock <= seuil / 2:
                etat, tag = t("CRITIQUE"), "critique"
            else:
                etat, tag = t("BAS"), "bas"
            tree.insert("", "end", tags=(tag,), values=(
                a["sku"],
                a.get("name") or a["sku"],
                _fmt(stock),
                _fmt(seuil),
                etat,
            ))
        tree.pack(fill="both", expand=True, padx=4, pady=4)
        sb = ttk.Scrollbar(cadre, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
