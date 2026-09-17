"""État du système — écran de diagnostic écrit pour un non-technicien.

L'entrepôt est tenu par des magasiniers, pas par des informaticiens. Savoir
« est-ce que tout va bien ? » demandait jusqu'ici de croiser plusieurs écrans
et des fichiers de journal. Cet écran répond à la question en une page : une
pastille de couleur par sujet (vert / orange / rouge) et UNE phrase en
français simple qui dit quoi en penser — jamais « backup_mirror_status:
failed », mais « la copie vers le disque de secours n'a pas fonctionné ».

Tout vient d'un seul appel serveur (`GET /systeme/etat`, réservé à l'admin),
SAUF la file d'attente locale : le nombre de bons non envoyés est propre à
CETTE machine, il est lu directement dans `file_attente` et n'existe nulle
part sur le serveur.

Les fonctions de calcul (`construire_indicateurs` et ses aides) sont pures et
n'ouvrent aucune fenêtre : c'est ce qui permet de tester les phrases affichées
sans démarrer Tk.
"""
import customtkinter as ctk

import file_attente
from async_call import run_async
from i18n import t

# Les trois niveaux, et leur couleur. Mêmes teintes que les bandeaux de
# main.py (rouge #b91c1c) pour que l'opérateur retrouve un code visuel connu.
VERT = "vert"
ORANGE = "orange"
ROUGE = "rouge"

COULEURS = {
    VERT: "#16a34a",
    ORANGE: "#b45309",
    ROUGE: "#b91c1c",
}

PASTILLES = {VERT: "●", ORANGE: "●", ROUGE: "●"}

LIBELLES_NIVEAU = {
    VERT: "Tout va bien",
    ORANGE: "À surveiller",
    ROUGE: "À traiter",
}

# Au-delà de ce nombre d'heures sans sauvegarde, l'indicateur passe au rouge.
# Le serveur renvoie son propre seuil (`seuil_heures`) : on l'utilise quand il
# est présent, cette constante n'est qu'un repli pour un serveur plus ancien.
SEUIL_SAUVEGARDE_H = 48


# --------------------------------------------------------------- formatage


def formater_age(heures) -> str:
    """« il y a 3 heures », en français simple et sans décimale inutile."""
    if heures is None:
        return t("à une date inconnue")
    try:
        heures = float(heures)
    except (TypeError, ValueError):
        return t("à une date inconnue")
    if heures < 0:
        heures = 0.0
    if heures < 1:
        minutes = int(round(heures * 60))
        if minutes <= 1:
            return t("il y a moins d'une minute")
        return t("il y a {n} minutes").format(n=minutes)
    if heures < 48:
        n = int(round(heures))
        return t("il y a 1 heure") if n == 1 else t("il y a {n} heures").format(n=n)
    jours = int(heures // 24)
    return t("il y a 1 jour") if jours == 1 else t("il y a {n} jours").format(n=jours)


def formater_taille(octets) -> str:
    """Taille de fichier lisible : « 12,4 Mo »."""
    if not isinstance(octets, (int, float)) or octets < 0:
        return t("taille inconnue")
    mo = octets / (1024 * 1024)
    if mo < 1:
        return t("{n} Ko").format(n=f"{octets / 1024:.0f}".replace(".", ","))
    return t("{n} Mo").format(n=f"{mo:.1f}".replace(".", ","))


def _indicateur(titre: str, niveau: str, phrase: str) -> dict:
    return {"titre": titre, "niveau": niveau, "phrase": phrase}


# ------------------------------------------------------------ indicateurs


def indicateur_serveur(joignable: bool) -> dict:
    if joignable:
        return _indicateur(
            t("Serveur"), VERT,
            t("Le serveur répond normalement : tout ce que vous saisissez "
              "part directement dans la base."))
    return _indicateur(
        t("Serveur"), ROUGE,
        t("Impossible de joindre le serveur. Vous pouvez continuer à "
          "travailler : les bons sont gardés sur ce poste et partiront tout "
          "seuls dès que la liaison revient. Prévenez le responsable si cela "
          "dure."))


def indicateur_sauvegarde(sauvegarde) -> dict:
    """Sauvegarde : la ligne la plus importante de l'écran.

    Ni le texte ni la couleur ne sont recalculés côté client : `perimee` et
    `seuil_heures` viennent du serveur (`backup.statut_sauvegarde`), c'est-à-
    dire de la MÊME logique que le bandeau rouge affiché à la connexion. Les
    deux ne peuvent donc pas se contredire.
    """
    titre = t("Sauvegarde des données")
    if not isinstance(sauvegarde, dict) or not sauvegarde:
        return _indicateur(
            titre, ORANGE,
            t("Impossible de savoir où en sont les sauvegardes. Signalez-le "
              "au responsable."))
    if sauvegarde.get("derniere_locale") is None:
        return _indicateur(
            titre, ROUGE,
            t("Aucune sauvegarde n'a été trouvée : si cet ordinateur tombe en "
              "panne, les données seraient perdues. À signaler tout de suite "
              "au responsable."))
    seuil = sauvegarde.get("seuil_heures") or SEUIL_SAUVEGARDE_H
    age = formater_age(sauvegarde.get("age_heures"))
    if sauvegarde.get("perimee"):
        return _indicateur(
            titre, ROUGE,
            t("Dernière sauvegarde réussie {age} — c'est trop vieux (plus de "
              "{h} h). Les saisies récentes ne sont pas encore protégées. À "
              "signaler au responsable.").format(age=age, h=seuil))
    return _indicateur(
        titre, VERT,
        t("Dernière sauvegarde réussie {age} — tout va bien, les données sont "
          "protégées.").format(age=age))


def indicateur_copie_secours(sauvegarde) -> dict:
    """Copie de la sauvegarde vers le disque/dossier miroir.

    Absent ou « disabled » : ce n'est pas une panne, la fonction n'est
    simplement pas configurée ici — on le dit, en gris (vert), sans alarmer.
    """
    titre = t("Copie de secours")
    statut = (sauvegarde or {}).get("statut_miroir") if isinstance(sauvegarde, dict) else None
    if statut in (None, "", "disabled"):
        return _indicateur(
            titre, VERT,
            t("Pas de deuxième copie configurée sur ce serveur. Ce n'est pas "
              "une panne."))
    if statut == "ok":
        return _indicateur(
            titre, VERT,
            t("La sauvegarde est aussi recopiée sur un deuxième emplacement — "
              "la dernière copie a réussi."))
    if statut == "en_attente":
        return _indicateur(
            titre, VERT,
            t("La deuxième copie est configurée ; aucune copie n'a encore été "
              "tentée depuis le démarrage du serveur."))
    return _indicateur(
        titre, ORANGE,
        t("La recopie de la sauvegarde vers le deuxième emplacement n'a pas "
          "fonctionné. La sauvegarde locale existe toujours ; à signaler au "
          "responsable."))


def indicateur_version(version_publiee, version_locale) -> dict:
    titre = t("Version du logiciel")
    if not version_publiee:
        return _indicateur(
            titre, VERT,
            t("Ce poste utilise la version {v}. Aucune nouvelle version n'est "
              "annoncée.").format(v=version_locale))
    if version_publiee == version_locale:
        return _indicateur(
            titre, VERT,
            t("Ce poste est à jour (version {v}).").format(v=version_locale))
    return _indicateur(
        titre, ORANGE,
        t("Une version plus récente existe ({neuve}) ; ce poste utilise "
          "encore la {vieille}. Rien d'urgent : demandez au responsable quand "
          "l'installer.").format(neuve=version_publiee, vieille=version_locale))


def indicateur_postes(nombre) -> dict:
    titre = t("Postes connectés")
    if not isinstance(nombre, int):
        return _indicateur(
            titre, ORANGE,
            t("Le serveur n'a pas su dire combien de postes sont connectés."))
    if nombre <= 0:
        return _indicateur(
            titre, ORANGE,
            t("Aucun poste n'est relié au serveur en direct. Les écrans se "
              "mettront à jour un peu moins vite, sans rien perdre."))
    if nombre == 1:
        return _indicateur(
            titre, VERT,
            t("1 poste est relié au serveur en direct."))
    return _indicateur(
        titre, VERT,
        t("{n} postes sont reliés au serveur en direct.").format(n=nombre))


def indicateur_file_locale(bons_en_attente) -> dict:
    """Bons non envoyés sur CE poste — information purement locale.

    Elle ne vient pas du serveur et ne peut pas en venir : ces bons sont dans
    un fichier de cette machine, et le serveur ignore jusqu'à leur existence.
    """
    titre = t("Bons en attente sur ce poste")
    if not isinstance(bons_en_attente, int) or bons_en_attente < 0:
        return _indicateur(
            titre, ORANGE,
            t("Impossible de lire la file d'attente de ce poste."))
    if bons_en_attente == 0:
        return _indicateur(
            titre, VERT,
            t("Aucun bon en attente : tout ce qui a été saisi sur ce poste "
              "est bien arrivé au serveur."))
    if bons_en_attente == 1:
        return _indicateur(
            titre, ORANGE,
            t("1 bon saisi sur ce poste n'est pas encore parti au serveur. Il "
              "partira tout seul ; n'éteignez pas cet ordinateur avant."))
    return _indicateur(
        titre, ORANGE,
        t("{n} bons saisis sur ce poste ne sont pas encore partis au serveur. "
          "Ils partiront tout seuls ; n'éteignez pas cet ordinateur "
          "avant.").format(n=bons_en_attente))


def indicateur_base(produits, taille_octets, version_schema) -> dict:
    titre = t("Base de données")
    if not isinstance(produits, int):
        return _indicateur(
            titre, ORANGE,
            t("La base de données n'a pas répondu à la lecture de contrôle. "
              "À signaler au responsable."))
    return _indicateur(
        titre, VERT,
        t("La base répond : {n} produits actifs au catalogue, fichier de "
          "{taille} (structure n° {schema}).").format(
              n=produits, taille=formater_taille(taille_octets),
              schema=version_schema if version_schema is not None else "?"))


def construire_indicateurs(etat, version_locale: str, bons_en_attente) -> list[dict]:
    """Liste ordonnée des indicateurs, du plus important au plus anecdotique.

    `etat` vide (dict vide ou None) = le serveur n'a pas répondu : on le dit
    en rouge sur la première ligne, et les lignes qui dépendent du serveur
    passent en orange « inconnu ». La file d'attente locale, elle, reste
    exacte : c'est justement dans ce cas qu'elle est utile.
    """
    etat = etat if isinstance(etat, dict) else {}
    joignable = bool(etat)
    sauvegarde = etat.get("sauvegarde")
    indicateurs = [
        indicateur_serveur(joignable),
        indicateur_sauvegarde(sauvegarde if joignable else None),
        indicateur_file_locale(bons_en_attente),
    ]
    if joignable:
        indicateurs += [
            indicateur_copie_secours(sauvegarde),
            indicateur_postes(etat.get("postes_connectes")),
            indicateur_version(etat.get("version_publiee"), version_locale),
            indicateur_base(etat.get("produits"), etat.get("taille_base_octets"),
                            etat.get("version_schema")),
        ]
    else:
        indicateurs.append(
            indicateur_version(None, version_locale))
    return indicateurs


def niveau_global(indicateurs) -> str:
    """Pire niveau rencontré : c'est le résumé affiché en haut de l'écran."""
    niveaux = {i.get("niveau") for i in indicateurs or []}
    if ROUGE in niveaux:
        return ROUGE
    if ORANGE in niveaux:
        return ORANGE
    return VERT


RESUMES = {
    VERT: "Tout va bien — rien à faire.",
    ORANGE: "Quelques points à surveiller (voir ci-dessous en orange).",
    ROUGE: "Attention : un point demande une action (voir en rouge).",
}


# ------------------------------------------------------------- checklists
# Réutilisées par main.py pour les bandeaux d'ouverture et de fermeture de
# session. Elles vivent ici parce qu'elles reposent exactement sur les mêmes
# signaux que l'écran : deux calculs séparés finiraient par se contredire.


def lignes_checklist_ouverture(bons_en_attente, nombre_ecarts) -> list[str]:
    """Points de contrôle du début de journée. Jamais bloquant, informatif.

    Renvoie une liste vide quand tout est normal : le bandeau ne s'affiche
    alors pas du tout. Un bandeau « tout va bien » tous les matins finirait
    par être fermé sans être lu — et le jour où il dit autre chose, personne
    ne le verrait.
    """
    lignes = []
    if isinstance(bons_en_attente, int) and bons_en_attente > 0:
        if bons_en_attente == 1:
            lignes.append(t("1 bon saisi hier sur ce poste n'est pas encore "
                            "parti au serveur — il repart automatiquement."))
        else:
            lignes.append(t("{n} bons saisis sur ce poste ne sont pas encore "
                            "partis au serveur — ils repartent "
                            "automatiquement.").format(n=bons_en_attente))
    if isinstance(nombre_ecarts, int) and nombre_ecarts > 0:
        if nombre_ecarts == 1:
            lignes.append(t("1 produit a un stock qui ne correspond plus à ses "
                            "mouvements — à vérifier dans la journée."))
        else:
            lignes.append(t("{n} produits ont un stock qui ne correspond plus "
                            "à leurs mouvements — à vérifier dans la "
                            "journée.").format(n=nombre_ecarts))
    return lignes


def lignes_checklist_fermeture(bons_en_attente, sauvegarde) -> list[str]:
    """Rappels avant de fermer l'application. Jamais bloquant non plus.

    Ne fait AUCUN appel réseau : `sauvegarde` est l'état déjà récupéré à la
    connexion. Interroger le serveur au moment où l'opérateur clique sur la
    croix ferait attendre la fenêtre sans raison.
    """
    lignes = []
    if isinstance(bons_en_attente, int) and bons_en_attente > 0:
        lignes.append(t("{n} bon(s) saisi(s) sur ce poste ne sont pas encore "
                        "partis au serveur. Ils repartiront au prochain "
                        "lancement.").format(n=bons_en_attente))
    if isinstance(sauvegarde, dict) and sauvegarde and sauvegarde.get("perimee"):
        seuil = sauvegarde.get("seuil_heures") or SEUIL_SAUVEGARDE_H
        if sauvegarde.get("derniere_locale") is None:
            lignes.append(t("Aucune sauvegarde des données n'a été trouvée — "
                            "à signaler au responsable demain matin."))
        else:
            lignes.append(t("La dernière sauvegarde date de plus de {h} h — à "
                            "signaler au responsable demain "
                            "matin.").format(h=seuil))
    return lignes


# ------------------------------------------------------------------ écran


class EtatSystemeView(ctk.CTkFrame):
    """Page en lecture seule. Aucun bouton d'action : on ne répare rien d'ici.

    Volontairement sans rafraîchissement automatique : c'est un écran qu'on
    ouvre pour vérifier, pas un moniteur laissé affiché en permanence, et
    aucun poste ne doit générer du trafic de fond sur le réseau de l'entrepôt.
    """

    def __init__(self, master, api, user_role=None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self.user_role = user_role
        self._cartes = []
        self._build()

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 8))
        ctk.CTkLabel(top, text=t("🩹  État du système"),
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")
        ctk.CTkButton(top, text=t("↻  Vérifier maintenant"), width=170,
                      fg_color="#2a2d35", hover_color="#353840",
                      corner_radius=8, command=self.refresh).pack(side="right", padx=8)

        self._resume = ctk.CTkLabel(
            self, text=t("Chargement..."), text_color="#6b7280",
            font=ctk.CTkFont(size=14, weight="bold"), anchor="w")
        self._resume.pack(fill="x", padx=20, pady=(0, 2))

        ctk.CTkLabel(
            self,
            text=t("Cette page ne modifie rien. Elle dit seulement si tout "
                   "fonctionne, en clair. Vert : rien à faire. Orange : à "
                   "surveiller. Rouge : prévenir le responsable."),
            text_color="#6b7280", font=ctk.CTkFont(size=11),
            wraplength=900, justify="left", anchor="w").pack(fill="x", padx=20,
                                                             pady=(0, 10))

        self._liste = ctk.CTkScrollableFrame(self, fg_color="transparent")
        self._liste.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        self._horodatage = ctk.CTkLabel(self, text="", text_color="#4b5563",
                                        font=ctk.CTkFont(size=10), anchor="w")
        self._horodatage.pack(fill="x", padx=20, pady=(0, 10))

    # ------------------------------------------------------------ données

    def refresh(self):
        self._resume.configure(text=t("Chargement..."), text_color="#6b7280")
        # `get_etat_systeme` avale déjà ses erreurs et renvoie {} : le rappel
        # d'erreur ne sert que pour l'imprévu (poste sans réseau du tout).
        run_async(self, self.api.get_etat_systeme,
                  self._afficher, lambda _e: self._afficher({}))

    def _bons_en_attente(self):
        """Lecture locale, jamais fatale : un disque en défaut ne doit pas
        empêcher d'afficher les six autres indicateurs."""
        try:
            return file_attente.nombre_en_attente()
        except Exception:
            return None

    def _afficher(self, etat):
        import version as client_version

        indicateurs = construire_indicateurs(
            etat, client_version.VERSION, self._bons_en_attente())
        global_ = niveau_global(indicateurs)
        self._resume.configure(text=t(RESUMES[global_]),
                               text_color=COULEURS[global_])

        for carte in self._cartes:
            carte.destroy()
        self._cartes = []

        for indic in indicateurs:
            couleur = COULEURS.get(indic["niveau"], "#6b7280")
            carte = ctk.CTkFrame(self._liste, fg_color="#1a1d23", corner_radius=10,
                                 border_width=1, border_color="#2a2d35")
            carte.pack(fill="x", padx=6, pady=5)
            self._cartes.append(carte)

            ligne = ctk.CTkFrame(carte, fg_color="transparent")
            ligne.pack(fill="x", padx=14, pady=(10, 0))
            ctk.CTkLabel(ligne, text=PASTILLES[indic["niveau"]],
                         text_color=couleur,
                         font=ctk.CTkFont(size=20)).pack(side="left", padx=(0, 10))
            ctk.CTkLabel(ligne, text=indic["titre"], text_color="#e5e7eb",
                         font=ctk.CTkFont(size=14, weight="bold")).pack(side="left")
            ctk.CTkLabel(ligne, text=t(LIBELLES_NIVEAU[indic["niveau"]]),
                         text_color=couleur,
                         font=ctk.CTkFont(size=11, weight="bold")).pack(side="right")

            ctk.CTkLabel(carte, text=indic["phrase"], text_color="#9ca3af",
                         font=ctk.CTkFont(size=12), wraplength=880,
                         justify="left", anchor="w").pack(fill="x", padx=(46, 14),
                                                          pady=(2, 12))

        horodatage = (etat or {}).get("horodatage")
        self._horodatage.configure(
            text=t("Vérifié auprès du serveur le {d}").format(d=horodatage)
            if horodatage else t("Le serveur n'a pas répondu à cette vérification."))
