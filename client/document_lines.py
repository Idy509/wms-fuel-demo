"""Gestion des lignes d'un bon, indépendante de l'interface graphique.

Séparée de la vue pour être testable sans affichage, et parce que l'ancienne
version indexait les lignes par position : en sélection multiple, les index
glissaient après chaque suppression et le bon transmis au serveur ne
correspondait plus à ce qui était affiché à l'écran.

Ici chaque ligne est identifiée par une clé stable (l'identifiant de la ligne
du tableau), jamais par sa position.
"""


def chercher_par_sku(products: list[dict], saisie: str) -> dict | None:
    """Produit dont le SKU correspond EXACTEMENT à la saisie (casse ignorée).

    Le scan d'une douchette valide par Entrée. Sans cette recherche exacte,
    scanner « AF1 » sélectionnait « AF10 » — la première correspondance
    partielle de la liste — et le mouvement partait sur le mauvais produit.
    """
    cible = (saisie or "").strip().lower()
    if not cible:
        return None
    for p in products:
        if str(p.get("sku", "")).strip().lower() == cible:
            return p
    return None


def resumer_lignes(lignes, produits_par_id: dict) -> dict:
    """Résumé du bon en cours : nombre de lignes, totaux par unité, valeur.

    `lignes` : les dicts de `LignesBon.toutes()` (product_id, quantity...).
    `produits_par_id` : le catalogue indexé par id, pour l'unité et le coût.

    Les totaux sont GROUPÉS PAR UNITÉ : additionner des gallons et des pièces
    donnerait un chiffre qui ne veut rien dire sur un bon mixte. Un produit
    absent du catalogue (bon repris avant la fin du chargement) tombe dans le
    groupe « pcs », l'unité par défaut du reste de l'écran.

    `valeur` ne vaut un nombre que si TOUS les produits du bon ont un coût
    unitaire renseigné : une valeur partielle, plus basse que la réalité,
    serait lue comme la valeur du bon et tromperait plus qu'elle n'informe.
    Sinon `valeur` vaut None et l'écran n'affiche aucun montant.
    """
    lignes = list(lignes)
    totaux: dict[str, float] = {}
    valeur = 0.0
    valeur_complete = True
    for l in lignes:
        produit = produits_par_id.get(l.get("product_id")) or {}
        unite = produit.get("unit") or "pcs"
        quantite = float(l.get("quantity") or 0)
        totaux[unite] = round(totaux.get(unite, 0.0) + quantite, 3)
        cout = produit.get("unit_cost")
        if cout is None:
            valeur_complete = False
        else:
            valeur += quantite * float(cout)
    return {
        "nb_lignes": len(lignes),
        "totaux_par_unite": sorted(totaux.items()),
        "valeur": round(valeur, 2) if (lignes and valeur_complete) else None,
    }


def texte_resume(resume: dict, traduire=None) -> str:
    """Résumé en une ligne, prêt pour le pied du tableau de saisie.

    `traduire` est la fonction de traduction (`i18n.t`) ; sans elle, le texte
    reste en français — le module doit rester importable sans l'interface.
    Renvoie une chaîne vide pour un bon vide : un pied de tableau qui annonce
    « 0 ligne » sur un écran neuf n'apprend rien à personne.
    """
    tr = traduire or (lambda s: s)
    if not resume["nb_lignes"]:
        return ""
    unites = " + ".join(f"{total:g} {unite}"
                        for unite, total in resume["totaux_par_unite"])
    texte = tr("{n} ligne(s) — {unites}").format(
        n=resume["nb_lignes"], unites=unites)
    if resume["valeur"] is not None:
        texte += "  —  " + tr("valeur estimée {valeur}").format(
            valeur=montant_lisible(resume["valeur"]))
    return texte


def montant_lisible(valeur: float) -> str:
    """« 1 250,50 » — espace pour les milliers, virgule décimale.

    Même convention que le reste des écrans (voir `views/utils._valeur_triable`,
    qui relit ce format pour trier une colonne, espace insécable comprise).
    L'espace des milliers est INSÉCABLE : sans elle, un pied de tableau étroit
    couperait le montant en deux morceaux illisibles.
    """
    return f"{valeur:,.2f}".replace(",", "\u00a0").replace(".", ",")


class LignesBon:
    def __init__(self):
        self._lignes: dict[str, dict] = {}

    def __len__(self) -> int:
        return len(self._lignes)

    def __bool__(self) -> bool:
        return bool(self._lignes)

    def ajouter(self, cle: str, product_id: int, sku: str, name: str, quantity: float) -> None:
        self._lignes[cle] = {
            "product_id": product_id,
            "sku": sku,
            "name": name,
            "quantity": quantity,
        }

    def retirer(self, cle: str) -> bool:
        return self._lignes.pop(cle, None) is not None

    def vider(self) -> None:
        self._lignes.clear()

    def cles(self) -> list[str]:
        return list(self._lignes)

    def toutes(self) -> list[dict]:
        return list(self._lignes.values())

    def payload(self) -> list[dict]:
        """Lignes au format attendu par l'API."""
        return [
            {"product_id": l["product_id"], "quantity": l["quantity"]}
            for l in self._lignes.values()
        ]

    def cle_pour_produit(self, product_id: int) -> str | None:
        """Clé de la première ligne portant ce produit, s'il est déjà saisi.

        Permet de fusionner une nouvelle saisie avec la ligne existante plutôt
        que d'afficher deux fois le même produit dans le bon.
        """
        for cle, ligne in self._lignes.items():
            if ligne["product_id"] == product_id:
                return cle
        return None

    def ligne(self, cle: str) -> dict | None:
        """La ligne portant cette clé, ou None. Lecture seule côté appelant."""
        return self._lignes.get(cle)

    def quantite(self, cle: str) -> float:
        return self._lignes[cle]["quantity"]

    def definir_quantite(self, cle: str, quantity: float) -> None:
        self._lignes[cle]["quantity"] = quantity

    def quantite_totale_hors(self, product_id: int, cle_exclue: str) -> float:
        """Total déjà saisi pour un produit, SANS compter une ligne donnée.

        Sert à la modification d'une ligne : vérifier le stock disponible en
        incluant la quantité que l'on est justement en train de remplacer
        refuserait une correction pourtant valable (passer 40 à 30 sur un
        stock de 35, par exemple).
        """
        return sum(l["quantity"] for cle, l in self._lignes.items()
                   if l["product_id"] == product_id and cle != cle_exclue)

    def quantite_totale(self, product_id: int) -> float:
        """Total déjà saisi pour un produit, toutes lignes confondues.

        Permet d'avertir l'opérateur avant l'envoi plutôt que d'essuyer un
        refus du serveur.
        """
        return sum(l["quantity"] for l in self._lignes.values() if l["product_id"] == product_id)
