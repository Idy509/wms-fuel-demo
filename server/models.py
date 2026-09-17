from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator

from horodatage import normaliser as normaliser_date
from precision import arrondir_saisie
from reference_data import (
    REGIONS,
    REGIONS_AVEC_ENTREPOT,
    TOUTES_REGIONS,
    a_entrepot_regional,
    normaliser_projet_fuel,
    normaliser_region,
    normaliser_supervisor,
    normaliser_technicien,
    regions_avec_entrepot_for,
)


# ------------------------------------------------ politique de mot de passe
#
# Deux niveaux, et deux seulement :
#
# - Comptes magasinier / lecteur / regional : 10 caractères. C'était 6, au
#   motif que ces comptes sont partagés dans un petit entrepôt et qu'un mot
#   de passe trop dur finirait sur un post-it. Six caractères se cassent hors
#   ligne en quelques minutes, et le serveur n'est plus seulement sur un LAN
#   de confiance : dix caractères restent dictables à l'oral (« bidon-vert-42 »)
#   tout en sortant du domaine du devinable.
#
# - Comptes ADMIN : 12 caractères minimum, dont au moins une lettre et un
#   chiffre. C'est le seul rôle qui peut créer des comptes, réinitialiser des
#   mots de passe, purger l'historique et lire les coûts d'achat.
#
# Dans les deux cas s'ajoutent deux refus, indépendants de la longueur : les
# mots de passe les plus courants (MOTS_DE_PASSE_INTERDITS) et ceux qui
# contiennent l'identifiant du compte.
#
# CHOIX ASSUMÉ : ni expiration, ni historique des mots de passe. Sur une
# équipe de 2-3 personnes, une expiration périodique ne produit que des mots
# de passe incrémentés (« Entrepot1 », « Entrepot2 »), et un historique
# demanderait de conserver d'anciens hachages sans bénéfice réel ici.
MDP_MIN_GENERAL = 10
MDP_MIN_ADMIN = 12
MESSAGE_MDP_ADMIN = (
    "Le mot de passe d'un compte administrateur doit contenir au moins "
    f"{MDP_MIN_ADMIN} caractères, dont au moins une lettre et un chiffre."
)
MESSAGE_MDP_COURANT = (
    "Ce mot de passe est trop courant : il figure dans la liste des premiers "
    "essais de n'importe quel logiciel d'attaque. Choisissez autre chose "
    "(deux ou trois mots sans rapport font un très bon mot de passe)."
)
MESSAGE_MDP_IDENTIFIANT = (
    "Le mot de passe ne doit pas contenir l'identifiant du compte : c'est le "
    "premier essai de quiconque connaît cet identifiant."
)

# Refusés d'office, comparaison insensible à la casse. Volontairement COURTE :
# ce n'est pas un dictionnaire d'attaque (des millions d'entrées, hors de
# propos ici), juste les essais qu'un logiciel tente en premier et ceux que
# l'on retrouve réellement dans un entrepôt — français, anglais et motifs
# clavier. Une liste plus longue n'ajouterait rien tant que la longueur
# minimale est de 10 caractères.
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
                        username: Optional[str] = None) -> Optional[str]:
    """Message d'erreur si le mot de passe ne convient pas au rôle, sinon None.

    `username` (facultatif) permet de refuser un mot de passe qui contient
    l'identifiant : « orelus2026 » sur le compte « orelus » a beau faire dix
    caractères, il ne résiste à rien.
    """
    if not mot_de_passe or len(mot_de_passe) < MDP_MIN_GENERAL:
        return (f"Le mot de passe doit contenir au moins "
                f"{MDP_MIN_GENERAL} caractères.")
    minuscule = mot_de_passe.lower()
    if minuscule in MOTS_DE_PASSE_INTERDITS:
        return MESSAGE_MDP_COURANT
    identifiant = (username or "").strip().lower()
    # 3 caractères : en dessous, l'identifiant est une syllabe qui se retrouve
    # par hasard dans n'importe quel mot, et le refus deviendrait incompréhensible.
    if len(identifiant) >= 3 and identifiant in minuscule:
        return MESSAGE_MDP_IDENTIFIANT
    if role != "admin":
        return None
    if (len(mot_de_passe) < MDP_MIN_ADMIN
            or not any(c.isalpha() for c in mot_de_passe)
            or not any(c.isdigit() for c in mot_de_passe)):
        return MESSAGE_MDP_ADMIN
    return None


# ------------------------------------------- seuil d'écart de comptage
#
# Au-delà de cet écart absolu entre le stock théorique et la quantité
# comptée, le comptage n'est plus accepté sur simple déclaration : le
# serveur refuse en 422 tant que `confirmation_ecart_important` n'est pas
# posé, et l'écran de comptage impose une SECONDE saisie à l'aveugle de la
# quantité avant de poser ce drapeau.
#
# La règle vit ici, et non dans app.py, parce que le client doit rendre
# exactement le même verdict AVANT l'aller-retour réseau : sinon l'opérateur
# retape une quantité que le serveur n'exigeait pas, ou pire, envoie sans
# double saisie un écart que le serveur va refuser. Le miroir client est
# `client/views/utils.seuil_ecart_important`, et un test compare les deux.
FACTEUR_ECART_IMPORTANT = 0.5
ECART_IMPORTANT_MINIMUM = 50.0


def seuil_ecart_important(theorique: float) -> float:
    """Écart absolu au-delà duquel un comptage doit être re-saisi.

    La moitié du stock théorique, avec un plancher de 50 unités : sur un
    produit à 4 gallons, un écart de 2 n'a rien d'anormal (une mesure de
    fond de bidon), alors que sur un produit à 800 il faut recompter.
    """
    return max(abs(float(theorique)) * FACTEUR_ECART_IMPORTANT,
               ECART_IMPORTANT_MINIMUM)


def _sans_espaces(v):
    """Retire les espaces de bord AVANT le contrôle de longueur.

    Posé en `mode="before"` : `Field(min_length=…)` s'applique alors à la
    valeur réellement enregistrée. Sans cela, un motif d'annulation de trois
    espaces satisfaisait `min_length=3` et un identifiant ' idens' entrait en
    base tel quel — l'index unique le distingue de 'idens' (COLLATE NOCASE ne
    normalise que la casse), et son titulaire ne pouvait plus se connecter.
    """
    return v.strip() if isinstance(v, str) else v


def _fini(v):
    """Refuse Inf/NaN sur un champ numérique facultatif, sans l'arrondir.

    Même motif qu'`arrondir_saisie` : `json.loads` accepte `1e999` et
    `Infinity`, et `Field(gt=0)` les laisse passer.
    """
    if v is None:
        return v
    arrondir_saisie(v)  # lève ValueError si Inf/NaN ; la valeur n'est pas arrondie
    return v


class ProductIn(BaseModel):
    sku: str = Field(max_length=64)
    name: str = Field(max_length=200)
    category: Optional[str] = Field(default=None, max_length=100)
    unit: str = Field(default="pcs", max_length=32)
    unit_type: Literal["piece", "volume"] = "piece"
    bidon_capacity: Optional[float] = Field(default=None, gt=0)
    # Capacité en gallons d'une bouteille consignée. NULL = pas de consigne.
    consigne_bouteille: Optional[float] = Field(default=None, gt=0)
    # Capacité de cuve (secteur FUEL, ex. 1300 gal pour le Diesel). NULL =
    # pas de plafond (ex. Gasoline). Une réception qui la dépasserait est
    # refusée par le serveur (server/app.py::create_document).
    tank_capacity: Optional[float] = Field(default=None, gt=0)
    # Site physique (secteur FUEL, ex. "WH Central", "Canapé-Vert"). NULL =
    # visible par tous les comptes du secteur (pas encore rattaché à un
    # site). Un compte non-admin avec un site défini ne voit et ne peut
    # transiger que sur les produits de SON site (ou sans site) — voir
    # `_site_du_compte` dans server/app.py. Un admin voit tout, quel que
    # soit son propre site.
    site: Optional[str] = Field(default=None, max_length=100)
    min_stock: float = Field(default=0, ge=0)
    current_stock: float = Field(default=0, ge=0)
    # Coût unitaire (devise de l'entrepôt), pour valoriser le stock. None =
    # non renseigné : le produit reste utilisable, seule sa valeur n'apparaît
    # pas dans les totaux (traitée comme 0 dans les agrégats).
    unit_cost: Optional[float] = Field(default=None, ge=0)
    # Code-barres imprimé sur l'article ou sur son étiquette de rayon. Facultatif :
    # aucun produit n'en a au départ, et un produit sans code se saisit à la main
    # comme avant. Deux produits actifs ne peuvent pas partager le même code.
    barcode: Optional[str] = Field(default=None, max_length=64)
    # Champs apportés par le Masterlist FON — sans usage pour Consumables
    # (restent NULL/False), au même titre que bidon_capacity/consigne_bouteille
    # qui ne concernent pas tous les produits.
    part_number: Optional[str] = Field(default=None, max_length=100)
    group_name: Optional[str] = Field(default=None, max_length=100)
    sub_category: Optional[str] = Field(default=None, max_length=100)
    supported_genset_models: Optional[str] = Field(default=None, max_length=200)
    vendor: Optional[str] = Field(default=None, max_length=200)
    rl_apply: bool = False
    discontinued: bool = False

    @field_validator("part_number", "group_name", "sub_category",
                      "supported_genset_models", "vendor", "site")
    @classmethod
    def _texte_facultatif_nettoye(cls, v):
        if v is None:
            return None
        return v.strip() or None

    @field_validator("min_stock", "current_stock")
    @classmethod
    def _arrondi(cls, v):
        return arrondir_saisie(v)

    @field_validator("barcode")
    @classmethod
    def _code_barres_nettoye(cls, v):
        # Une douchette envoie parfois un espace ou un retour chariot résiduel :
        # sans strip, le code scanné ne retrouverait jamais la fiche enregistrée.
        # Vide == pas de code : NULL, jamais '' (l'index unique partiel exclut
        # les deux, mais NULL est la seule forme lisible en base).
        if v is None:
            return None
        return v.strip() or None

    @field_validator("unit_cost")
    @classmethod
    def _arrondi_cout(cls, v):
        if v is not None:
            return round(arrondir_saisie(v), 4)
        return v

    @field_validator("bidon_capacity", "consigne_bouteille", "tank_capacity")
    @classmethod
    def _capacite_finie(cls, v):
        return _fini(v)

    @field_validator("sku", "name", "unit")
    @classmethod
    def _texte_nettoye(cls, v):
        # Sans strip, ' ABC-1' et 'ABC-1' passent l'index unique du SKU : deux
        # fiches pour un seul article réel, et un stock coupé en deux.
        nettoye = (v or "").strip()
        if not nettoye:
            raise ValueError("Ce champ ne peut pas être vide")
        return nettoye

    @field_validator("category")
    @classmethod
    def _categorie_nettoyee(cls, v):
        # Facultative, mais nettoyée comme les autres champs texte : sinon
        # ' Lubrifiants' et 'Lubrifiants' forment deux catégories distinctes
        # dans les regroupements du tableau de bord.
        if v is None:
            return None
        nettoye = v.strip()
        return nettoye or None

    @model_validator(mode="after")
    def _coherence_unite(self):
        if self.unit_type == "volume" and self.unit == "pcs":
            self.unit = "gls"
        if self.unit_type == "piece":
            self.bidon_capacity = None
        return self


class ProductOut(BaseModel):
    id: int
    sku: str
    name: str
    category: Optional[str] = None
    unit: str
    unit_type: str = "piece"
    bidon_capacity: Optional[float] = None
    # Capacité en gallons d'une bouteille consignée. NULL = pas de consigne.
    consigne_bouteille: Optional[float] = None
    # Capacité de cuve (secteur FUEL). NULL = pas de plafond.
    tank_capacity: Optional[float] = None
    # Site physique (secteur FUEL). NULL = visible par tous les comptes.
    site: Optional[str] = None
    initial_stock: float
    initial_stock_date: Optional[str] = None
    current_stock: float
    min_stock: float
    unit_cost: Optional[float] = None
    # Code-barres, NULL tant qu'aucun n'a été associé à la fiche.
    barcode: Optional[str] = None
    part_number: Optional[str] = None
    group_name: Optional[str] = None
    sub_category: Optional[str] = None
    supported_genset_models: Optional[str] = None
    vendor: Optional[str] = None
    rl_apply: bool = False
    discontinued: bool = False
    archived: int = 0
    # Nom du fichier image sur le disque du serveur (jamais son contenu, ni le
    # nom d'origine envoyé par le poste). NULL = pas de photo. Le client s'en
    # sert uniquement pour savoir s'il faut appeler GET /products/{id}/photo.
    photo_path: Optional[str] = None
    created_at: str


class DocumentLineIn(BaseModel):
    product_id: int
    quantity: float = Field(gt=0)

    @field_validator("quantity")
    @classmethod
    def _arrondi(cls, v):
        arrondi = arrondir_saisie(v)
        if arrondi <= 0:
            # Ex. 0.0001 passe gt=0 mais s'arrondit à 0 : mieux vaut refuser
            # que d'enregistrer une ligne à quantité nulle.
            raise ValueError(f"Quantité trop petite pour être représentée ({v})")
        return arrondi


class DocumentIn(BaseModel):
    # SUPPLIER_RETURN : marchandise non conforme renvoyée à un fournisseur.
    # Distinct de RETURN (qui rapatrie du stock terrain DEPUIS un
    # superviseur) : ici le stock quitte l'entrepôt, sans région ni
    # superviseur associé — seulement un contact fournisseur.
    type: Literal["RECEIVING", "DELIVERY", "RETURN", "SUPPLIER_RETURN"]
    # Longueurs bornées : ces champs sont relus dans l'historique, l'export
    # Excel et le bordereau imprimé. Sans borne, une valeur collée par erreur
    # (des milliers de caractères) était stockée puis renvoyée sur chaque page
    # de l'historique et dans chaque export.
    party: Optional[str] = Field(default=None, max_length=200)
    # Optionnel : rattache le bon à une fiche contacts (historique, éviter les
    # doublons de saisie). `party` reste utilisable seul pour un contact non
    # encore enregistré.
    party_contact_id: Optional[int] = None
    operator: Optional[str] = Field(default=None, max_length=100)
    region: Optional[str] = None
    reference: Optional[str] = Field(default=None, max_length=100)
    note: Optional[str] = Field(default=None, max_length=2000)
    created_at: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=100)
    # Opérateur qui saisit. Distinct de `operator`, qui porte le superviseur
    # sur une expédition : sans ce champ l'auteur de la saisie était perdu.
    created_by: Optional[str] = Field(default=None, max_length=100)
    carrier: Optional[str] = Field(default=None, max_length=200)
    # Technicien terrain livré individuellement (eau distillée). Optionnel :
    # une expédition ordinaire vers une région n'en a pas. Sert au suivi
    # nominatif de la consigne bouteille, en plus du suivi par région.
    technician: Optional[str] = Field(None, max_length=200)
    # Champs Fuel (secteur FUEL uniquement) : sans région ni superviseur, une
    # livraison de carburant s'organise autour d'un véhicule et d'un
    # « projet » (étiquette budgétaire interne, sans rapport avec les
    # secteurs applicatifs de même nom). NULL/absents pour Consumables/FON,
    # comme `technician` l'est déjà pour FON.
    vehicle_plate: Optional[str] = Field(default=None, max_length=20)
    vehicle_info: Optional[str] = Field(default=None, max_length=200)
    mileage: Optional[float] = Field(default=None, ge=0)
    project: Optional[str] = Field(default=None, max_length=100)
    receiver: Optional[str] = Field(default=None, max_length=200)
    fuel_card: Optional[str] = Field(default=None, max_length=100)
    lines: list[DocumentLineIn]

    @field_validator("region")
    @classmethod
    def _region_canonique(cls, v):
        # Contrôle large uniquement (pas de secteur connu ici) : la région
        # est obligatoire pour DELIVERY/RETURN seulement pour les secteurs
        # qui utilisent le système région/superviseur (Consumables, FON) —
        # cette règle, sector-conditionnelle, est appliquée dans
        # `server/app.py::create_document`, une fois le secteur de l'auteur
        # connu.
        return normaliser_region(v)

    @field_validator("technician")
    @classmethod
    def _technicien_canonique(cls, v):
        return normaliser_technicien(v)

    @field_validator("vehicle_plate", "vehicle_info", "receiver", "fuel_card", mode="before")
    @classmethod
    def _texte_fuel_nettoye(cls, v):
        if v is None:
            return None
        return v.strip() or None

    @field_validator("project")
    @classmethod
    def _projet_canonique(cls, v):
        return normaliser_projet_fuel(v)

    @field_validator("created_at")
    @classmethod
    def _date_normalisee(cls, v):
        # Refuse une date illisible plutôt que de la stocker : le tri et les
        # regroupements par mois ne sauraient pas l'interpréter.
        return normaliser_date(v)

    @model_validator(mode="after")
    def _region_obligatoire_en_expedition(self):
        # Une sortie Consumables/FON doit être attribuable à une région :
        # sans elle, le stock sorti n'est rattachable à aucun superviseur.
        # Ce modèle n'a pas le secteur de l'auteur sous la main (pas de
        # session/DB ici) : l'obligation elle-même (région requise pour
        # DELIVERY/RETURN chez Consumables/FON, mais pas chez FUEL, qui n'a
        # ni région ni superviseur) est vérifiée côté serveur, une fois le
        # secteur connu — voir `server/app.py::create_document`.
        #
        # La cohérence région/superviseur (le superviseur correspond-il
        # vraiment à cette région) n'est PLUS vérifiée ici : elle dépendait
        # de `supervisor_est_valide` sans secteur, dont l'union multi-secteurs
        # devient FAUSSE dès que deux secteurs réutilisent le même nom de
        # région avec des sens différents (ex. « Petit-Goâve » : alias
        # Consumables vers Aquin, vraie région RAN à part) — la région, déjà
        # canonicalisée SANS secteur par `_region_canonique` ci-dessus, peut
        # alors ne plus correspondre au secteur réel de l'auteur, et rejetait
        # à tort un couple région/superviseur RAN pourtant valide (constaté
        # en réel le 13 septembre 2026). La vérification qui fait autorité,
        # avec le vrai secteur, est déjà faite dans
        # `server/app.py::create_document` (`supervisor_est_valide(region,
        # operator, secteur)`) — la dupliquer ici sans secteur n'apporte plus
        # rien et casse des cas légitimes.
        libelle = {"DELIVERY": "une expédition", "RETURN": "un retour"}
        if self.type in libelle and self.region and self.operator:
            # `operator` représente le superviseur (l'auteur réel de la saisie
            # est `created_by`) : contrôle orthographique large seulement.
            self.operator = normaliser_supervisor(self.operator)
        if self.type == "SUPPLIER_RETURN" and not self.party and not self.party_contact_id:
            # Sans destinataire identifié (texte ou fiche contact), un retour
            # fournisseur n'est rattachable à personne — contrairement à une
            # réception, qui vient légitimement de « l'entrepôt central ».
            raise ValueError(
                "Le fournisseur destinataire est obligatoire pour un retour fournisseur "
                "(party ou party_contact_id)"
            )
        return self


class DocumentLineOut(BaseModel):
    id: int
    product_id: int
    sku: str
    name: str
    quantity: float
    counted_quantity: Optional[float] = None
    # Quantité confirmée reçue par l'entrepôt régional. NULL tant que le
    # transfert est en transit. Colonne dédiée, distincte de counted_quantity
    # (comptage physique d'un ajustement) : deux sens dans un même champ
    # rendraient l'historique illisible.
    received_quantity: Optional[float] = None


class AdjustmentLineIn(BaseModel):
    product_id: int
    # L'opérateur saisit ce qu'il a COMPTÉ, jamais l'écart : c'est le serveur
    # qui calcule la correction. Compter est fiable, soustraire ne l'est pas.
    counted_quantity: float = Field(ge=0)

    @field_validator("counted_quantity")
    @classmethod
    def _arrondi(cls, v):
        return arrondir_saisie(v)


class AdjustmentIn(BaseModel):
    # Même motif que CancelIn : '   ' passait min_length=3 et l'écart
    # d'inventaire restait sans explication.
    reason: str = Field(min_length=3, max_length=500,
                        description="Motif de l'ajustement, obligatoire")
    operator: Optional[str] = Field(default=None, max_length=100)
    region: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=2000)
    created_at: Optional[str] = None
    idempotency_key: Optional[str] = Field(default=None, max_length=100)
    created_by: Optional[str] = Field(default=None, max_length=100)
    confirmation_ecart_important: bool = False
    lines: list[AdjustmentLineIn]

    @field_validator("reason", "operator", mode="before")
    @classmethod
    def _nettoyer(cls, v):
        return _sans_espaces(v)

    @field_validator("region")
    @classmethod
    def _region_canonique(cls, v):
        return normaliser_region(v)

    @field_validator("created_at")
    @classmethod
    def _date_normalisee(cls, v):
        return normaliser_date(v)


class CancelIn(BaseModel):
    # Le motif est la seule justification d'une extourne de stock, et il est
    # relu dans l'historique et le journal d'audit. Nettoyé AVANT le contrôle
    # de longueur : '   ' satisfaisait min_length=3 et l'annulation passait
    # avec un motif vide.
    reason: str = Field(min_length=3, max_length=500,
                        description="Motif de l'annulation, obligatoire")
    operator: Optional[str] = Field(default=None, max_length=100)

    @field_validator("reason", "operator", mode="before")
    @classmethod
    def _nettoyer(cls, v):
        return _sans_espaces(v)


class DocumentOut(BaseModel):
    id: int
    type: str
    party: Optional[str]
    party_contact_id: Optional[int] = None
    operator: Optional[str]
    region: Optional[str] = None
    # Région d'ORIGINE d'un transfert région -> région. NULL partout ailleurs :
    # l'origine implicite de tous les autres types est l'entrepôt central.
    source_region: Optional[str] = None
    reference: Optional[str] = None
    note: Optional[str]
    created_at: str
    created_by: Optional[str] = None
    station: Optional[str] = None
    server_received_at: Optional[str] = None
    carrier: Optional[str] = None
    technician: Optional[str] = None
    vehicle_plate: Optional[str] = None
    vehicle_info: Optional[str] = None
    mileage: Optional[float] = None
    project: Optional[str] = None
    receiver: Optional[str] = None
    fuel_card: Optional[str] = None
    cancelled_at: Optional[str] = None
    cancelled_by: Optional[str] = None
    cancel_reason: Optional[str] = None
    # Transfert vers un entrepôt régional : NULL = encore en transit.
    received_at: Optional[str] = None
    received_by: Optional[str] = None
    lines: list[DocumentLineOut]
    # Informatif : produits passés sous leur seuil mini après ce mouvement.
    alertes_seuil: Optional[list[dict]] = None
    # Informatif : avertissements non bloquants (ex. retour supérieur à l'expédié).
    avertissements: Optional[list[str]] = None
    # Informatif : écarts constatés à la confirmation de réception régionale.
    # Jamais bloquants — ce qui compte pour le stock régional est le reçu.
    ecarts_reception: Optional[list[dict]] = None


class ProductUpdate(BaseModel):
    initial_stock: Optional[float] = Field(default=None, ge=0)
    initial_stock_date: Optional[str] = None
    sku: Optional[str] = Field(default=None, max_length=64)
    name: Optional[str] = Field(default=None, max_length=200)
    unit: Optional[str] = Field(default=None, max_length=32)
    # Catégorie : elle n'était modifiable nulle part après la création (elle
    # est déduite du nom à la création). Une fiche sans catégorie faussait les
    # regroupements du tableau de bord sans qu'aucun écran ne permette de la
    # corriger — c'est le principal défaut listé par /products/qualite.
    category: Optional[str] = Field(default=None, max_length=100)
    # Même domaine que ProductIn : un PATCH ne doit pas pouvoir installer une
    # valeur que la création refuse ('Volume', 'litre'...), sinon le client ne
    # sait plus interpréter l'unité du produit.
    unit_type: Optional[Literal["piece", "volume"]] = None
    # None explicite efface la valeur en base (distinction via exclude_unset).
    # gt=0 n'est vérifié que si une valeur non-None est fournie : une capacité
    # ou une consigne à 0 (ou négative) rendrait toute conversion absurde.
    bidon_capacity: Optional[float] = Field(default=None, gt=0)
    consigne_bouteille: Optional[float] = Field(default=None, gt=0)
    tank_capacity: Optional[float] = Field(default=None, gt=0)
    site: Optional[str] = Field(default=None, max_length=100)
    min_stock: Optional[float] = Field(default=None, ge=0)
    unit_cost: Optional[float] = Field(default=None, ge=0)
    # Code-barres. Même règle que `category` : None explicite EFFACE le code
    # (un code mal associé doit pouvoir être retiré), champ absent = inchangé.
    barcode: Optional[str] = Field(default=None, max_length=64)
    # Archivage réversible. Sans ce champ, un produit archivé par erreur était
    # définitivement perdu pour l'application : PATCH refusait {"archived": 0}
    # en 400 « Aucun champ à modifier », et le SKU restant pris (index unique),
    # impossible non plus de le recréer.
    archived: Optional[int] = Field(default=None, ge=0, le=1)
    # Champs FON (voir ProductIn) : absents = inchangés, None explicite efface.
    part_number: Optional[str] = Field(default=None, max_length=100)
    group_name: Optional[str] = Field(default=None, max_length=100)
    sub_category: Optional[str] = Field(default=None, max_length=100)
    supported_genset_models: Optional[str] = Field(default=None, max_length=200)
    vendor: Optional[str] = Field(default=None, max_length=200)
    rl_apply: Optional[bool] = None
    discontinued: Optional[bool] = None

    @field_validator("part_number", "group_name", "sub_category",
                      "supported_genset_models", "vendor", "site")
    @classmethod
    def _texte_facultatif_nettoye(cls, v):
        if v is None:
            return None
        return v.strip() or None

    @field_validator("initial_stock", "min_stock")
    @classmethod
    def _arrondi(cls, v):
        if v is not None:
            return arrondir_saisie(v)
        return v

    @field_validator("unit_cost")
    @classmethod
    def _arrondi_cout(cls, v):
        if v is not None:
            return round(arrondir_saisie(v), 4)
        return v

    @field_validator("bidon_capacity", "consigne_bouteille", "tank_capacity")
    @classmethod
    def _capacite_finie(cls, v):
        return _fini(v)

    @field_validator("sku", "name", "unit")
    @classmethod
    def _texte_nettoye(cls, v):
        # Sans strip, ' ABC-1' et 'ABC-1' cohabitent : deux produits distincts
        # pour l'index unique, un seul pour le magasinier.
        if v is None:
            return v
        nettoye = v.strip()
        if not nettoye:
            raise ValueError("Ce champ ne peut pas être vide")
        return nettoye

    @field_validator("category")
    @classmethod
    def _categorie_nettoyee(cls, v):
        # Contrairement au SKU et au nom, une catégorie VIDE est licite : elle
        # efface le classement d'un produit qui n'en a pas.
        if v is None:
            return None
        return v.strip() or None

    @field_validator("barcode")
    @classmethod
    def _code_barres_nettoye(cls, v):
        # Comme la catégorie : un code vide est licite et vaut « retire le
        # code ». Le strip protège des espaces envoyés par une douchette.
        if v is None:
            return None
        return v.strip() or None

    @model_validator(mode="after")
    def _coherence_unite(self):
        # Même règle que ProductIn, mais seulement quand le PATCH touche
        # explicitement unit_type : sinon une modification de nom écraserait
        # l'unité ou la capacité bidon déjà en base.
        if "unit_type" not in self.model_fields_set:
            return self
        if self.unit_type == "volume" and self.unit == "pcs":
            # L'affectation marque le champ comme "set" : il part bien au PATCH.
            self.unit = "gls"
        if self.unit_type == "piece":
            self.bidon_capacity = None
        return self


class BottleReturnIn(BaseModel):
    """Retour physique de bouteilles vides par une région.

    Ne touche pas au stock : seul le solde de contenants dus est diminué.
    """
    product_id: int
    region: str
    bottles: float = Field(gt=0)
    note: Optional[str] = Field(default=None, max_length=2000)
    created_by: Optional[str] = Field(default=None, max_length=100)
    # Numéro de la fiche papier signée à la reprise des bouteilles : sert de
    # pièce justificative quand un solde est contesté.
    reference: Optional[str] = Field(default=None, max_length=100)
    # Renseigné quand ce sont les bouteilles d'un technicien nommé qui
    # reviennent : sans lui, sa dette personnelle ne diminuerait jamais.
    technician: Optional[str] = None

    @field_validator("bottles")
    @classmethod
    def _bouteilles_finies(cls, v):
        # _fini et non arrondir_saisie : la dette est calculée par DIVISION
        # (quantité / consigne). Avec une consigne de 3 gallons, elle vaut
        # 3,333... — arrondir la saisie à 3 décimales rendrait le retour
        # intégral impossible, il resterait toujours un résidu dû.
        return _fini(v)

    @field_validator("region")
    @classmethod
    def _region_canonique(cls, v):
        canonique = normaliser_region(v)
        if not canonique:
            raise ValueError("La région est obligatoire")
        return canonique

    @field_validator("technician")
    @classmethod
    def _technicien_canonique(cls, v):
        return normaliser_technicien(v)

    @field_validator("reference")
    @classmethod
    def _reference_nettoyee(cls, v):
        # Une chaîne vide envoyée par le formulaire vaut « pas de fiche ».
        return (v or "").strip() or None


class RlReturnIn(BaseModel):
    """Retour physique d'un équipement en reverse logistics (ex. routeur GPON FON).

    Même principe que `BottleReturnIn` : ne touche pas au stock, seul le
    solde d'unités dues (livrées et pas encore retournées) est diminué.
    """
    product_id: int
    region: str
    quantity: float = Field(gt=0)
    note: Optional[str] = Field(default=None, max_length=2000)
    created_by: Optional[str] = Field(default=None, max_length=100)
    # Numéro de série ou preuve de retour, comme la fiche papier des
    # bouteilles : sert de pièce justificative quand un solde est contesté.
    reference: Optional[str] = Field(default=None, max_length=100)
    # FON n'a pas de référentiel de techniciens (TECHNICIANS_PAR_SECTEUR est
    # vide pour ce secteur) : contrairement à `BottleReturnIn.technician`, ce
    # champ n'est pas validé contre une liste fermée, juste nettoyé.
    technician: Optional[str] = Field(default=None, max_length=200)

    @field_validator("quantity")
    @classmethod
    def _quantite_finie(cls, v):
        return _fini(v)

    @field_validator("region")
    @classmethod
    def _region_canonique(cls, v):
        canonique = normaliser_region(v)
        if not canonique:
            raise ValueError("La région est obligatoire")
        return canonique

    @field_validator("technician", "reference", mode="before")
    @classmethod
    def _nettoyer_texte(cls, v):
        return (v or "").strip() or None


class ThresholdUpdate(BaseModel):
    product_id: int
    min_stock: float = Field(ge=0)

    @field_validator("min_stock")
    @classmethod
    def _arrondi(cls, v):
        return arrondir_saisie(v)


# ------------------------------------------------------- secteurs (FON...)
#
# Consumables (l'entrepôt actuel) et FON (puis RAN/Fuel/Spare) partagent la
# même base et le même code, sans jamais mélanger leurs données. Chaque
# compte appartient à UN secteur ; le serveur relit ce secteur en base à
# chaque requête (comme `users.region` pour un compte régional) et ne
# renvoie/n'écrit jamais que les lignes `products`/`documents` de ce secteur.
#
# Liste en Python et non en CHECK SQL : comme `region`, ajouter RAN/Fuel/
# Spare plus tard ne doit demander aucune migration, juste une valeur de plus
# ici.
SECTEURS_VALIDES = {"CONSUMABLES", "FON", "RAN", "SPARES", "FUEL"}
SECTEUR_DEFAUT = "CONSUMABLES"

# Comptes autorisés à AGIR HORS DE LEUR SECTEUR sur l'administration des
# utilisateurs : créer un compte dans un autre secteur (POST /users) et lister
# les comptes de tous les secteurs (GET /users?sector=ALL).
#
# C'est le rôle d'amorçage : Idy509 (admin Consumables) ouvre les tout premiers
# comptes FON/RAN/Fuel/Spare avant que ces secteurs n'aient leur propre admin.
# Ce cas existait déjà, mais SANS être écrit nulle part : n'IMPORTE quel admin
# pouvait créer un admin dans un autre secteur, donc s'ouvrir un accès complet
# à des données qui ne le regardent pas. La liste rend l'exception visible et
# la ferme à tous les autres.
#
# Comparaison insensible à la casse (l'identifiant l'est déjà à la connexion,
# COLLATE NOCASE) : voir `est_admin_multi_secteurs`.
ADMINS_MULTI_SECTEURS = {"Idy509"}


def est_admin_multi_secteurs(username: Optional[str]) -> bool:
    """Vrai si ce compte peut administrer les comptes d'un AUTRE secteur."""
    if not username:
        return False
    return username.strip().lower() in {
        nom.lower() for nom in ADMINS_MULTI_SECTEURS
    }


def normaliser_secteur(v: Optional[str]) -> str:
    secteur = (v or "").strip().upper()
    if not secteur:
        return SECTEUR_DEFAUT
    if secteur not in SECTEURS_VALIDES:
        raise ValueError(
            f"Secteur inconnu : « {v} » (secteurs possibles : "
            f"{', '.join(sorted(SECTEURS_VALIDES))})"
        )
    return secteur


class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=50)
    password: str = Field(min_length=6, max_length=128)
    # Nom affiché partout : liste des comptes, journal d'audit, `created_by`
    # des bons, bordereaux imprimés. Borné comme les autres champs affichés.
    display_name: str = Field(min_length=2, max_length=100)
    # 'regional' : compte d'un entrepôt régional. Il ne voit que SA région et
    # ne saisit que sa réception, son inventaire et ses alertes.
    role: Literal["admin", "magasinier", "lecteur", "regional", "board"] = "magasinier"
    # Obligatoire pour un compte régional, ignoré (mis à None) sinon : sans
    # rattachement, un compte régional ne saurait pas quels transferts confirmer.
    region: Optional[str] = None
    # Secteur d'appartenance (Consumables, FON...). Défaut Consumables : les
    # écrans existants qui créent un compte sans y penser encore ne changent
    # pas de comportement.
    sector: str = SECTEUR_DEFAUT
    # Site physique (secteur FUEL, ex. "WH Central", "Canapé-Vert"). NULL =
    # aucune restriction (voir `products.site`). Un compte non-admin avec un
    # site défini ne voit et ne peut transiger que sur les produits de SON
    # site (ou sans site) ; un admin voit tout, quel que soit son site.
    site: Optional[str] = Field(default=None, max_length=100)

    @field_validator("sector")
    @classmethod
    def _secteur_canonique(cls, v):
        return normaliser_secteur(v)

    @field_validator("site", mode="before")
    @classmethod
    def _site_nettoye(cls, v):
        if v is None:
            return None
        return v.strip() or None

    # L'identifiant est comparé exactement à la connexion
    # (`WHERE username = ? COLLATE NOCASE`) : COLLATE NOCASE ignore la casse,
    # pas les espaces. ' idens' créait donc un second compte, distinct de
    # 'idens' pour l'index unique, dont le titulaire ne pouvait plus se
    # connecter — cinq essais et le compte se verrouillait 5 minutes.
    @field_validator("username", "display_name", mode="before")
    @classmethod
    def _nettoyer(cls, v):
        return _sans_espaces(v)

    @model_validator(mode="after")
    def _mot_de_passe_selon_role(self):
        # Contrôlé ici et non par un Field : la règle dépend du rôle, qui est
        # un autre champ du même modèle.
        erreur = erreur_mot_de_passe(self.password, self.role,
                                     username=self.username)
        if erreur:
            raise ValueError(erreur)
        return self

    @model_validator(mode="after")
    def _region_selon_role(self):
        # `sector` est un champ du même modèle (validé juste avant, les
        # field_validator passent avant les model_validator) : la
        # vérification peut donc être STRICTE ici, contrairement à
        # DocumentIn/RegionNoteIn/RegionalTransferIn qui n'ont pas le secteur
        # sous la main et se contentent d'un contrôle large, complété
        # ensuite côté serveur.
        if self.role == "regional":
            region = normaliser_region(self.region, self.sector)
            if not region:
                raise ValueError("La région est obligatoire pour un compte régional")
            if not a_entrepot_regional(region, self.sector):
                raise ValueError(
                    f"{region} n'a pas d'entrepôt régional pour le secteur "
                    f"{self.sector} (régions possibles : "
                    f"{', '.join(regions_avec_entrepot_for(self.sector))})"
                )
            self.region = region
        else:
            self.region = None
        return self


class UserUpdate(BaseModel):
    # Mêmes bornes qu'à la création : sans elles, un PUT posant
    # `{"display_name": "   "}` remplaçait le nom du compte par du vide.
    # Le compte devenait une ligne anonyme dans la liste des utilisateurs, le
    # journal d'audit et le `created_by` de tous ses bons suivants.
    display_name: Optional[str] = Field(default=None, min_length=2, max_length=100)
    role: Optional[Literal["admin", "magasinier", "lecteur", "regional", "board"]] = None
    active: Optional[bool] = None
    password: Optional[str] = Field(default=None, min_length=6, max_length=128)
    region: Optional[str] = None
    # Pas de champ `sector` ici, volontairement : un compte est créé dans un
    # secteur et n'en change pas — le permettre ouvrirait la porte à un admin
    # déplaçant un compte hors du périmètre que `PUT /users/{id}` lui-même
    # vérifie (voir `server/app.py`).
    #
    # `site`, lui, reste modifiable : ce n'est pas une frontière de sécurité
    # comme le secteur, juste une étiquette qui filtre le catalogue visible
    # (voir `products.site`) — un compte peut changer de site physique.
    site: Optional[str] = Field(default=None, max_length=100)

    @field_validator("display_name", mode="before")
    @classmethod
    def _nettoyer(cls, v):
        return _sans_espaces(v)

    @field_validator("site", mode="before")
    @classmethod
    def _site_nettoye(cls, v):
        if v is None:
            return None
        return v.strip() or None

    @field_validator("region")
    @classmethod
    def _region_canonique(cls, v):
        return normaliser_region(v)

    @model_validator(mode="after")
    def _region_selon_role(self):
        # Un changement de rôle vers 'regional' doit s'accompagner d'une
        # région : sinon le compte serait rattaché à rien et ne verrait aucun
        # transfert, sans qu'aucune erreur ne le signale.
        if self.role == "regional":
            if not self.region:
                raise ValueError("La région est obligatoire pour un compte régional")
            if not a_entrepot_regional(self.region):
                raise ValueError(
                    f"{self.region} n'a pas d'entrepôt régional "
                    f"(régions possibles : {', '.join(REGIONS_AVEC_ENTREPOT)})"
                )
        elif self.role is not None:
            self.region = None
        elif self.region is not None:
            # Changement de région SEUL (le rôle n'est pas dans la charge) :
            # le compte reste régional côté serveur, mais rien ne vérifiait
            # la région envoyée. On pouvait rattacher un compte régional à
            # Port-au-Prince, qui n'a pas d'entrepôt régional : le compte ne
            # voyait plus jamais un transfert, sans la moindre erreur.
            if not a_entrepot_regional(self.region):
                raise ValueError(
                    f"{self.region} n'a pas d'entrepôt régional "
                    f"(régions possibles : {', '.join(REGIONS_AVEC_ENTREPOT)})"
                )
        return self


class UserOut(BaseModel):
    id: int
    username: str
    display_name: str
    role: str
    region: Optional[str] = None
    sector: str = SECTEUR_DEFAUT
    site: Optional[str] = None
    active: bool
    created_at: str


class UserRegionsIn(BaseModel):
    """Régions auxquelles un compte lecteur est restreint.

    Liste VIDE = aucune restriction : le lecteur voit de nouveau toutes les
    régions. C'est l'état par défaut de tous les comptes existants, et la
    seule façon de lever une restriction posée par erreur.

    Contrairement au rôle 'regional' (une seule région, colonne
    `users.region`), un lecteur peut en surveiller plusieurs — d'où une liste.
    Port-au-Prince est acceptée ici : elle n'a pas d'entrepôt régional, mais
    elle porte bien des bons dans l'historique, qu'un lecteur de zone doit
    pouvoir consulter.
    """
    # Ce modèle ne connaît pas le secteur du compte visé (il vit en base,
    # relu dans l'endpoint) : le contrôle ici est large (toutes régions, tous
    # secteurs confondus), la vérification stricte par secteur se fait dans
    # `server/app.py::definir_regions_utilisateur`.
    regions: list[str] = Field(default_factory=list, max_length=len(TOUTES_REGIONS))

    @field_validator("regions")
    @classmethod
    def _regions_canoniques(cls, v):
        canoniques: list[str] = []
        for brute in v:
            region = normaliser_region(brute)
            if not region:
                raise ValueError("Région vide dans la liste")
            if region not in TOUTES_REGIONS:
                raise ValueError(
                    f"Région inconnue : {brute} "
                    f"(régions possibles : {', '.join(TOUTES_REGIONS)})"
                )
            # Doublons silencieusement ignorés : la clé primaire de
            # user_regions les refuserait, avec une erreur SQL illisible.
            if region not in canoniques:
                canoniques.append(region)
        return canoniques


class RegionNoteIn(BaseModel):
    """Annotation libre d'une région (saisonnalité, accès dégradé).

    Purement informative : rien dans le système ne la lit pour décider quoi
    que ce soit. Une note vide efface l'annotation.
    """
    region: str
    # Bornée comme les autres champs de texte affichés : cette note est
    # rendue telle quelle dans un écran, une note de 100 000 caractères
    # rendrait l'écran inutilisable.
    note: str = Field(default="", max_length=500)

    @field_validator("note", mode="before")
    @classmethod
    def _nettoyer_note(cls, v):
        return _sans_espaces(v)

    @field_validator("region")
    @classmethod
    def _region_canonique(cls, v):
        # Contrôle large (pas de secteur connu ici) : la vérification stricte
        # « cette région appartient au secteur de l'auteur » se fait dans
        # `server/app.py::definir_note_region`.
        region = normaliser_region(v)
        if not region or region not in TOUTES_REGIONS:
            raise ValueError(
                f"Région inconnue : {v} (régions possibles : {', '.join(TOUTES_REGIONS)})"
            )
        return region


class ReceptionLineIn(BaseModel):
    product_id: int
    # Ce que la région a RÉELLEMENT reçu. Zéro est une valeur légitime :
    # le colis peut n'être jamais arrivé.
    received_quantity: float = Field(ge=0)

    @field_validator("received_quantity")
    @classmethod
    def _arrondi(cls, v):
        return arrondir_saisie(v)


class RegionalTransferIn(BaseModel):
    """Envoi de marchandise d'un entrepôt régional vers un autre.

    `region_source` est ignorée pour un compte régional : sa propre région
    fait foi, comme pour l'inventaire et les signalements. Elle n'est utile
    qu'à un compte central qui saisit pour le compte d'une région (reprise
    d'un transfert convenu par téléphone).

    La destination est obligatoire dans tous les cas : c'est la seule chose
    que le serveur ne peut pas deviner.
    """
    region_destination: str
    region_source: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=2000)
    reference: Optional[str] = Field(default=None, max_length=100)
    carrier: Optional[str] = Field(default=None, max_length=200)
    idempotency_key: Optional[str] = Field(default=None, max_length=100)
    # Date du DÉPART réel, posée par le client. Sans elle, un transfert saisi
    # hors ligne et rejoué deux jours plus tard s'afficherait daté du jour de
    # la reprise réseau — même raison que pour l'inventaire régional.
    created_at: Optional[str] = None
    lines: list[DocumentLineIn]

    @field_validator("region_destination", "region_source")
    @classmethod
    def _region_canonique(cls, v):
        return normaliser_region(v)

    @field_validator("created_at")
    @classmethod
    def _date_normalisee(cls, v):
        return normaliser_date(v)

    @model_validator(mode="after")
    def _destination_valide(self):
        if not self.region_destination:
            raise ValueError("La région destinataire est obligatoire")
        if not a_entrepot_regional(self.region_destination):
            raise ValueError(
                f"{self.region_destination} n'a pas d'entrepôt régional : elle est "
                f"servie directement par l'entrepôt central. Régions possibles : "
                f"{', '.join(REGIONS_AVEC_ENTREPOT)}"
            )
        if self.region_source and self.region_source == self.region_destination:
            raise ValueError(
                "La région d'origine et la région destinataire sont identiques"
            )
        if not self.lines:
            raise ValueError("Le transfert doit contenir au moins une ligne")
        return self


class ReceptionConfirmIn(BaseModel):
    lines: list[ReceptionLineIn]
    # Ajoutée à la note du bon : bornée comme celle de DocumentIn.
    note: Optional[str] = Field(default=None, max_length=2000)


class RegionalInventoryLineIn(BaseModel):
    product_id: int
    quantity: float = Field(ge=0)

    @field_validator("quantity")
    @classmethod
    def _arrondi(cls, v):
        return arrondir_saisie(v)


class RegionalInventoryIn(BaseModel):
    """Comptage déclaratif d'une région. Purement informatif.

    Aucun écart n'est calculé et aucun stock n'est ajusté : ce rapport sert à
    l'administrateur central pour décider du réapprovisionnement.
    """
    # Ignorée pour un compte régional (sa propre région fait foi). Seul un
    # compte régional peut désormais envoyer un comptage : le champ ne sert
    # plus qu'aux appels sans authentification (premier lancement, tests).
    region: Optional[str] = None
    note: Optional[str] = Field(default=None, max_length=2000)
    lines: list[RegionalInventoryLineIn]
    # Date du COMPTAGE PHYSIQUE, pas de l'envoi réseau : posée par le client au
    # moment de la saisie, avant une éventuelle mise en file d'attente hors
    # ligne. Sans elle, un comptage fait un jour sans connexion et rejoué deux
    # jours plus tard s'affichait daté du jour de la reprise réseau — trompeur
    # pour juger si un comptage est à jour.
    created_at: Optional[str] = None
    # MULTI-01 : même protection que sur les bons et les transferts régionaux
    # (RegionalTransferIn.idempotency_key). Sans elle, un comptage rejoué
    # depuis la file d'attente hors ligne (réponse perdue après coupure)
    # créait un second rapport, doublant le total agrégé du dernier comptage.
    idempotency_key: Optional[str] = Field(default=None, max_length=100)

    @field_validator("idempotency_key")
    @classmethod
    def _cle_vide_vaut_absente(cls, v):
        # '' n'est PAS NULL pour l'index unique partiel (WHERE idempotency_key
        # IS NOT NULL) : sans cette normalisation, deux comptages envoyés avec
        # '' au lieu d'une absence de champ entreraient en collision sur le
        # même index que deux VRAIS doublons.
        return v.strip() or None if v is not None else None

    @field_validator("region")
    @classmethod
    def _region_canonique(cls, v):
        return normaliser_region(v)

    @field_validator("created_at")
    @classmethod
    def _date_normalisee(cls, v):
        return normaliser_date(v)


class RegionalAlertSignalIn(BaseModel):
    """Signalement manuel : « ce produit est bas / en rupture dans ma région ».

    Pas de gravité ni de priorité, volontairement : un signalement existe ou
    n'existe pas, il est ouvert ou résolu. Ajouter des niveaux obligerait le
    magasinier à choisir, et ce choix ne servirait à personne.
    """
    product_id: int
    note: Optional[str] = Field(default=None, max_length=2000)
    # Ignorée pour un compte régional (sa propre région fait foi) ; utile à un
    # administrateur qui signale pour le compte d'une région.
    region: Optional[str] = None

    @field_validator("region")
    @classmethod
    def _region_canonique(cls, v):
        return normaliser_region(v)


class LoginIn(BaseModel):
    username: str = Field(max_length=50)
    password: str = Field(max_length=128)


class PasswordChangeIn(BaseModel):
    """Corps de PATCH /me/password.

    L'endpoint recevait un `dict` brut : n'importe quel type et n'importe
    quelle taille passaient jusqu'au code métier, et rien ne documentait le
    contrat côté client. Les bornes suivent celles de `LoginIn`.
    """
    old_password: str = Field(default="", max_length=128)
    new_password: str = Field(default="", max_length=128)


class StockDateIn(BaseModel):
    """Corps de PATCH /products/stock-date.

    `date` reste facultative ici pour que l'absence produise le 400 « Date
    requise » de l'endpoint (message lisible par le magasinier) plutôt qu'un
    422 générique de validation.
    """
    date: Optional[str] = Field(default=None, max_length=10)
    reset_initial: bool = False


# ------------------------------------------------------------------ contacts
#
# Champs libres de la fiche contact. Chacun est borné : sans modèle, ces
# endpoints acceptaient un `dict` brut, donc une adresse d'un mégaoctet ou une
# valeur d'un type inattendu, rangée telle quelle dans SQLite (qui ne vérifie
# pas les types). Les longueurs suivent celles déjà retenues ailleurs pour des
# champs équivalents (nom 200, notes 2000).
class ContactIn(BaseModel):
    # Sans `min_length` : un nom vide doit produire le 400 « Le nom est
    # obligatoire » de l'endpoint, pas un 422 de validation illisible.
    name: str = Field(default="", max_length=200)
    type: str = Field(default="other", max_length=20)
    phone: Optional[str] = Field(default=None, max_length=50)
    email: Optional[str] = Field(default=None, max_length=200)
    address: Optional[str] = Field(default=None, max_length=500)
    notes: Optional[str] = Field(default=None, max_length=2000)
    devise: Optional[str] = Field(default=None, max_length=20)
    conditions_paiement: Optional[str] = Field(default=None, max_length=200)
    whatsapp: Optional[str] = Field(default=None, max_length=50)
    # Laissé volontairement souple (entier, chaîne vide, chaîne numérique) :
    # le formulaire client envoie une chaîne vide quand l'opérateur efface le
    # champ. La normalisation et le message d'erreur restent dans
    # `app._delai_jours_valide`, seul endroit à connaître la borne métier.
    delai_jours: Optional[int | float | str] = None

    @field_validator("name", mode="before")
    @classmethod
    def _nom_nettoye(cls, v):
        return _sans_espaces(v)


class ContactUpdate(BaseModel):
    """Mise à jour partielle : seuls les champs réellement envoyés comptent.

    `model_dump(exclude_unset=True)` côté endpoint distingue « pas envoyé » de
    « envoyé à vide », exactement comme le faisait le `in body` d'avant.
    """
    name: Optional[str] = Field(default=None, max_length=200)
    type: Optional[str] = Field(default=None, max_length=20)
    active: Optional[bool] = None
    phone: Optional[str] = Field(default=None, max_length=50)
    email: Optional[str] = Field(default=None, max_length=200)
    address: Optional[str] = Field(default=None, max_length=500)
    notes: Optional[str] = Field(default=None, max_length=2000)
    devise: Optional[str] = Field(default=None, max_length=20)
    conditions_paiement: Optional[str] = Field(default=None, max_length=200)
    whatsapp: Optional[str] = Field(default=None, max_length=50)
    delai_jours: Optional[int | float | str] = None

    @field_validator("name", mode="before")
    @classmethod
    def _nom_nettoye(cls, v):
        return _sans_espaces(v)
