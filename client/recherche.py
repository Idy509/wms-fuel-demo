"""Recherche d'article tolérante aux fautes de frappe.

Un magasinier tape « filre » ou « huil » et n'obtenait rien : la recherche des
écrans Produits, Réception, Inventaire régional et Alertes régionales était un
simple `requete in nom.lower()`. Une lettre de travers, un accent oublié, et la
liste se vidait — sans indiquer que le produit existe pourtant.

Deux règles, volontairement différentes selon le champ :

- **SKU** — un code, pas du texte. « AF1 » ne doit JAMAIS ramener « AF10 » par
  approximation : on garde la correspondance littérale (contient / commence
  par), sans la moindre tolérance. Un code approché est un mauvais code.
- **Nom** — du texte libre saisi par un humain, lu par un autre humain. C'est
  là que la tolérance s'applique : accents retirés, et une à deux fautes
  admises selon la longueur du mot cherché.

Aucune dépendance externe : `unicodedata` pour les accents et une distance de
Levenshtein bornée écrite à la main. Le coût est mesuré pour être invisible sur
quelques centaines d'articles refiltrés à chaque frappe — la distance s'arrête
dès que le budget d'erreurs est dépassé, et n'est même pas tentée quand la
correspondance littérale suffit déjà.
"""
import unicodedata

# Budget de fautes admises, en fonction de la longueur du texte cherché.
# Rien en dessous de 4 caractères : sur « oil », une faute admise ramènerait
# « ail », « oi », « pil »… tout le catalogue. Le seuil monte à 2 seulement
# pour les mots longs, où deux lettres de travers restent reconnaissables.
def _budget_fautes(requete: str) -> int:
    if len(requete) < 4:
        return 0
    if len(requete) < 7:
        return 1
    return 2


def normaliser(texte) -> str:
    """Minuscules, sans accents, espaces compactés.

    Accepte n'importe quoi (None, un nombre venant d'une colonne de base) :
    cette fonction est appelée sur des données métier, elle ne doit jamais
    faire tomber l'écran qui filtre.
    """
    if texte is None:
        return ""
    if not isinstance(texte, str):
        texte = str(texte)
    # NFD sépare la lettre de son accent, on jette les accents (catégorie Mn).
    decompose = unicodedata.normalize("NFD", texte.lower())
    sans_accent = "".join(c for c in decompose
                          if unicodedata.category(c) != "Mn")
    return " ".join(sans_accent.split())


def distance_bornee(a: str, b: str, budget: int) -> int:
    """Distance de Levenshtein, abandonnée dès qu'elle dépasse `budget`.

    Renvoie `budget + 1` pour « trop loin », sans finir le calcul : c'est ce
    qui rend le filtrage assez rapide pour tourner à chaque frappe.
    """
    if abs(len(a) - len(b)) > budget:
        return budget + 1
    if a == b:
        return 0
    precedente = list(range(len(b) + 1))
    for i, ca in enumerate(a, start=1):
        courante = [i]
        minimum = i
        for j, cb in enumerate(b, start=1):
            cout = 0 if ca == cb else 1
            valeur = min(precedente[j] + 1,        # suppression
                         courante[j - 1] + 1,      # insertion
                         precedente[j - 1] + cout)  # substitution
            courante.append(valeur)
            if valeur < minimum:
                minimum = valeur
        if minimum > budget:
            return budget + 1
        precedente = courante
    return precedente[-1]


def _mot_approche(requete: str, mot: str, budget: int) -> bool:
    """Vrai si `mot` ressemble à `requete` à `budget` fautes près.

    La comparaison porte aussi sur le DÉBUT du mot : on cherche « filre » et le
    produit s'appelle « Filtre à huile » — il faut confronter « filre » à
    « filtr », pas au mot entier, sinon la différence de longueur seule fait
    échouer. C'est ce qui rend la recherche utilisable en cours de frappe.
    """
    if distance_bornee(requete, mot, budget) <= budget:
        return True
    # Fenêtre de tête, élargie du budget pour absorber une lettre en trop.
    for longueur in range(len(requete), len(requete) + budget + 1):
        if longueur >= len(mot):
            break
        if distance_bornee(requete, mot[:longueur], budget) <= budget:
            return True
    return False


def _jeton_dans_nom(jeton: str, nom_normalise: str, mots: list[str]) -> bool:
    if jeton in nom_normalise:
        return True
    budget = _budget_fautes(jeton)
    if not budget:
        return False
    return any(_mot_approche(jeton, mot, budget) for mot in mots)


def correspond(requete, sku, nom) -> bool:
    """Vrai si l'article (sku, nom) répond à la recherche saisie.

    Une requête vide correspond à tout : c'est la liste complète, pas une
    liste vide.
    """
    q = normaliser(requete)
    if not q:
        return True

    sku_n = normaliser(sku)
    # Le code d'abord, littéralement : c'est le chemin du scan de code-barres
    # et du magasinier qui connaît la référence par cœur.
    if sku_n and q in sku_n:
        return True

    nom_n = normaliser(nom)
    if q in nom_n:
        return True
    if not nom_n:
        return False

    # Recherche multi-mots : « huile moteur » doit trouver « Oil Engine
    # (huile moteur 15W40) » quel que soit l'ordre. Chaque mot tapé doit
    # retrouver son correspondant, sinon on ramènerait n'importe quoi.
    mots = nom_n.split()
    return all(_jeton_dans_nom(jeton, nom_n, mots) for jeton in q.split())


def filtrer(articles, requete, cle_sku: str = "sku", cle_nom: str = "name") -> list:
    """Filtre une liste de dictionnaires produit. Ordre d'origine préservé."""
    q = normaliser(requete)
    if not q:
        return list(articles)
    return [a for a in articles
            if correspond(q, a.get(cle_sku), a.get(cle_nom))]


def correspond_libelle(requete, libelle) -> bool:
    """Variante pour les listes déjà mises en forme (« SKU - Nom (stock…) »).

    Le combo de saisie d'un bon ne manipule pas des dictionnaires mais les
    chaînes affichées. Le SKU en est le premier segment, avant « - ».
    """
    texte = libelle if isinstance(libelle, str) else str(libelle or "")
    sku, _, reste = texte.partition(" - ")
    return correspond(requete, sku, reste or texte)
