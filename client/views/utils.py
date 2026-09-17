import math

# ------------------------------------------------ politique de mot de passe
#
# Miroir EXACT de `server/models.erreur_mot_de_passe` : le serveur reste seul
# juge (il refuse en 422/400), mais l'écran doit pouvoir le dire avant
# l'aller-retour réseau, en français et sous le champ concerné.
#
# 10 caractères pour les comptes magasinier / lecteur / regional, 12 avec au
# moins une lettre et un chiffre pour les comptes ADMIN, seuls habilités à
# créer des comptes et à réinitialiser des mots de passe. S'y ajoutent deux
# refus : les mots de passe les plus courants, et ceux qui contiennent
# l'identifiant du compte. Ni expiration, ni historique : voir le commentaire
# côté serveur.
MDP_MIN_GENERAL = 10
MDP_MIN_ADMIN = 12

# Miroir de `server/models.MOTS_DE_PASSE_INTERDITS`.
MOTS_DE_PASSE_INTERDITS = frozenset({
    "password", "password1", "password123", "passw0rd", "motdepasse",
    "motdepasse1", "motdepasse123", "123456", "1234567", "12345678",
    "123456789", "1234567890", "0123456789", "111111", "000000", "abc123",
    "azerty", "azertyuiop", "qwerty", "qwertyuiop", "qwerty123", "azerty123",
    "wxcvbn", "zxcvbn", "1qaz2wsx", "iloveyou", "admin", "admin123",
    "administrateur", "administrator", "root", "toor", "welcome",
    "bienvenue", "bonjour", "soleil", "chouchou", "coucou", "secret",
    "letmein", "changeme", "changezmoi", "entrepot", "entrepot1",
    "entrepot123", "warehouse", "warehouse1", "warehouse123", "magasin",
    "magasinier", "stock123", "haiti", "haiti123",
})


def erreur_mot_de_passe(mot_de_passe: str, role: str,
                        username: str | None = None) -> str | None:
    """Message d'erreur en français, ou None si le mot de passe convient."""
    if not mot_de_passe or len(mot_de_passe) < MDP_MIN_GENERAL:
        return f"{MDP_MIN_GENERAL} caractères minimum"
    minuscule = mot_de_passe.lower()
    if minuscule in MOTS_DE_PASSE_INTERDITS:
        return "Mot de passe trop courant, choisissez-en un autre"
    identifiant = (username or "").strip().lower()
    if len(identifiant) >= 3 and identifiant in minuscule:
        return "Le mot de passe ne doit pas contenir l'identifiant"
    if role != "admin":
        return None
    if (len(mot_de_passe) < MDP_MIN_ADMIN
            or not any(c.isalpha() for c in mot_de_passe)
            or not any(c.isdigit() for c in mot_de_passe)):
        return (f"Compte administrateur : {MDP_MIN_ADMIN} caractères minimum, "
                "dont au moins une lettre et un chiffre")
    return None


# ------------------------------------------- seuil d'écart de comptage
#
# Miroir EXACT de `server/models.seuil_ecart_important`. L'écran de comptage
# s'en sert pour savoir, AVANT d'envoyer, quels produits imposent une seconde
# saisie à l'aveugle de la quantité. Un désaccord avec le serveur se paierait
# soit par une double saisie inutile, soit par un refus 422 après coup.
FACTEUR_ECART_IMPORTANT = 0.5
ECART_IMPORTANT_MINIMUM = 50.0


def seuil_ecart_important(theorique: float) -> float:
    """Écart absolu au-delà duquel la quantité comptée doit être re-saisie."""
    return max(abs(float(theorique)) * FACTEUR_ECART_IMPORTANT,
               ECART_IMPORTANT_MINIMUM)


def ecarts_a_re_saisir(entrees) -> list[dict]:
    """Comptages dont l'écart impose une SECONDE saisie à l'aveugle.

    `entrees` : les dictionnaires de comptage de l'écran d'inventaire, avec au
    moins `theorique` et `compte`.

    L'ancienne version se contentait d'une question « Confirmez-vous ce
    comptage ? » à laquelle on répond oui d'un clic — c'est-à-dire d'une
    confirmation qui ne vérifiait rien. Retaper le chiffre, lui, attrape la
    faute de frappe (un 0 en trop, deux chiffres inversés) qui est la cause
    réelle de la quasi-totalité de ces écarts.

    Cette fonction vit dans `utils` et non dans la vue pour rester vérifiable
    sans ouvrir de fenêtre Tk.
    """
    a_verifier = []
    for v in entrees:
        compte = v.get("compte")
        if compte is None:
            continue
        theorique = v.get("theorique") or 0
        if abs(compte - theorique) > seuil_ecart_important(theorique):
            a_verifier.append(v)
    return a_verifier


def saisies_concordent(premiere, seconde) -> bool:
    """Vrai si les deux saisies indépendantes donnent la même quantité.

    Tolérance 1e-9 : les quantités sont des flottants (gallons au dixième),
    une égalité stricte refuserait deux saisies pourtant identiques.
    """
    try:
        a, b = float(premiere), float(seconde)
    except (TypeError, ValueError):
        return False
    if not (math.isfinite(a) and math.isfinite(b)):
        return False
    return abs(a - b) < 1e-9


def float_saisie(texte) -> float:
    """`float()` pour une saisie d'opérateur : virgule acceptée, Inf/NaN refusés.

    Lève `ValueError` exactement comme `float()`, pour se glisser sans rien
    changer dans les `try: ... except ValueError:` déjà en place partout.

    Deux pièges que `float()` seul ne couvre pas :

    * la virgule décimale — un magasinier tape « 12,5 », pas « 12.5 » ;
    * `float("inf")`, `float("Infinity")`, `float("nan")` : ce sont des
      conversions VALIDES en Python. Une saisie « nan » traversait donc toutes
      les vérifications des écrans (`nan <= 0`, `nan < 0` et `nan > stock`
      valent toutes False : NaN n'est comparable à rien) et partait au
      serveur. Le serveur la refuse désormais (precision.arrondir_saisie),
      mais l'opérateur récupérait un message de validation serveur au lieu
      d'un « Quantité invalide » posé sous son champ. `inf` traversait de la
      même façon et se retrouvait sérialisé en `Infinity`, qui n'est pas du
      JSON valide.
    """
    valeur = float(str(texte).strip().replace(",", "."))
    if not math.isfinite(valeur):
        raise ValueError(f"valeur non finie : {texte!r}")
    return valeur


def _valeur_triable(texte: str) -> float:
    """Convertit une cellule affichée en nombre, ou lève ValueError.

    Dans cette appli, l'espace sépare les milliers et la virgule est une
    DÉCIMALE (voir `_fmt` de la vue d'ensemble : 12345 -> '12 345',
    12.5 -> '12,5'). L'ancienne version supprimait la virgule comme un
    séparateur de milliers : la colonne « Stock » de la table de couverture
    triait alors 12,5 comme 125, donc au-dessus de 100.
    """
    valeur = texte.strip().replace(" ", "").replace("\xa0", "").replace(" ", "")
    if "." in valeur:
        # Point décimal déjà présent : une virgule ne peut être qu'un
        # séparateur de milliers.
        valeur = valeur.replace(",", "")
    elif valeur.count(",") == 1:
        valeur = valeur.replace(",", ".")
    else:
        valeur = valeur.replace(",", "")
    return float(valeur)


def sort_treeview(tree, col, reverse=False):
    data = [(tree.set(k, col), k) for k in tree.get_children("")]
    try:
        data.sort(key=lambda t: _valeur_triable(t[0]), reverse=reverse)
    except ValueError:
        data.sort(key=lambda t: t[0].lower(), reverse=reverse)
    for i, (_, k) in enumerate(data):
        tree.move(k, "", i)
    tree.heading(col, command=lambda: sort_treeview(tree, col, not reverse))
