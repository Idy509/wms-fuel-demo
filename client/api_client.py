from pathlib import Path
from typing import Optional
from urllib.parse import quote

import requests


class ApiError(Exception):
    # Code HTTP à l'origine de l'erreur, quand elle vient d'une réponse du
    # serveur. None pour tout le reste (serveur injoignable, timeout) et pour
    # les ApiError levées ailleurs dans le client : un appelant qui distingue
    # un cas précis (ex. un 404 « code-barres inconnu », qui n'est pas une
    # panne) doit toujours lire cet attribut par getattr, jamais le supposer.
    status_code = None


class SessionExpiredError(ApiError):
    pass


_CHAMPS_LISIBLES = {
    "sku": "Code article (SKU)",
    "name": "Nom",
    "unit": "Unité",
    "unit_type": "Type d'unité",
    "bidon_capacity": "Capacité du bidon",
    "consigne_bouteille": "Consigne bouteille (gls/bouteille)",
    "bottles": "Nombre de bouteilles",
    "region": "Région",
    "current_stock": "Stock actuel",
    "initial_stock": "Stock de début",
    "min_stock": "Seuil minimum",
    "quantity": "Quantité",
    "password": "Mot de passe",
    "username": "Identifiant",
    "display_name": "Nom affiché",
    "role": "Rôle",
    "lines": "Lignes",
    "type": "Type",
}


def _formater_erreurs_validation(erreurs: list) -> str:
    """Traduit une réponse 422 Pydantic en une phrase lisible par un magasinier.

    Le JSON brut ([{'type': 'greater_than_equal', 'loc': ['body', ...]}])
    n'apprend rien à quelqu'un qui n'écrit pas de code.
    """
    messages = []
    for err in erreurs[:3]:
        if not isinstance(err, dict):
            messages.append(str(err))
            continue
        msg = err.get("msg") or err.get("type") or "valeur invalide"
        loc = [str(p) for p in (err.get("loc") or []) if p != "body"]
        champ = loc[-1] if loc else ""
        libelle = _CHAMPS_LISIBLES.get(champ, champ)
        messages.append(f"{libelle} : {msg}" if libelle else str(msg))
    if len(erreurs) > 3:
        messages.append(f"… et {len(erreurs) - 3} autre(s) erreur(s)")
    return "\n".join(messages) or "Données invalides"


def _extraire_detail(resp) -> str:
    """Message d'erreur serveur, ramené à une phrase affichable."""
    try:
        detail = resp.json().get("detail", resp.text)
    except Exception:
        return resp.text
    # FastAPI renvoie une liste d'erreurs Pydantic pour un 422 de validation.
    if isinstance(detail, list):
        return _formater_erreurs_validation(detail)
    if isinstance(detail, dict):
        return str(detail.get("msg") or detail.get("message") or detail)
    return str(detail)


def _message_serveur_injoignable(base_url: str, exc: Exception) -> str:
    """Panne réseau expliquée à un magasinier, pas à un développeur.

    Le message affiché était la `RequestException` brute :
    « HTTPConnectionPool(host='192.168.1.5', port=8000): Max retries exceeded
    with url: /login (Caused by NewConnectionError(...)) ». Sur le poste
    central, où l'adresse est bonne depuis toujours, cela n'arrivait presque
    jamais. Sur un poste régional installé à distance, c'est au contraire
    l'erreur la PLUS probable au premier lancement — l'adresse du serveur y
    est saisie à la main — et l'opérateur ne pouvait rien en déduire.

    ATTENTION : la locution « contacter le serveur » est un point de
    branchement, pas seulement de la prose. `file_attente.rejouer`,
    `document_form._on_submit_error` et `inventory` la cherchent dans le texte
    de l'erreur pour distinguer une panne réseau — bon mis en file d'attente —
    d'un refus métier — bon rejeté. La retirer d'ici enverrait les bons des
    postes hors ligne à la poubelle au lieu de la file.
    """
    if isinstance(exc, requests.exceptions.Timeout):
        cause = "Le serveur a mis trop de temps à répondre."
    elif isinstance(exc, requests.exceptions.SSLError):
        cause = "La liaison sécurisée avec le serveur a été refusée."
    else:
        cause = "Rien ne répond à cette adresse."
    return (
        "Impossible de contacter le serveur.\n"
        f"{cause}\n\n"
        f"Adresse utilisée : {base_url}\n\n"
        "À vérifier, dans cet ordre :\n"
        "1. Le poste est-il bien connecté à Internet ?\n"
        "2. L'adresse du serveur est-elle la bonne ? "
        "Elle se corrige dans « Paramètres », en bas à gauche.\n"
        "3. Si tout semble correct, préviens l'entrepôt central : "
        "le serveur est peut-être arrêté."
    )


class ApiClient:
    # (connexion, lecture). Le défaut vise un poste DISTANT, pas le poste
    # central : 2 s de poignée de main TCP suffisaient sur le même
    # commutateur, mais faisaient échouer en « serveur injoignable » des
    # envois qui auraient abouti sur une liaison Internet lente. Le vrai
    # réglage vient de la configuration du poste (`config.timeouts`) ; cette
    # valeur ne sert qu'aux appelants qui n'en fournissent pas.
    def __init__(self, base_url: str, timeout: tuple[float, float] = (10.0, 60.0),
                  user_token: str = ""):
        self.base_url = base_url
        self.timeout = timeout
        self.user_token = user_token
        # Identité du compte connecté : posée par main.py après un login
        # réussi. Sert à étiqueter la file d'attente hors ligne
        # (file_attente.py) — un poste partagé entre comptes ne doit jamais
        # rejouer sous une session le bon d'un autre.
        self.username = ""
        # Une Session réutilise la connexion TCP entre appels au lieu d'en
        # ouvrir une nouvelle à chaque requête — sensible sur la vue Stock
        # (rafraîchissement toutes les 30s) et à l'ouverture d'un formulaire
        # qui enchaîne plusieurs appels (produits, contacts, référentiel).
        self._session = requests.Session()

    def _request(self, method: str, path: str, **kwargs):
        url = f"{self.base_url}{path}"
        entetes = dict(kwargs.pop("headers", None) or {})
        entetes["ngrok-skip-browser-warning"] = "1"
        if self.user_token:
            entetes["X-User-Token"] = self.user_token
        kwargs["headers"] = entetes
        try:
            resp = self._session.request(method, url, timeout=self.timeout, **kwargs)
        except requests.exceptions.RequestException as e:
            raise ApiError(_message_serveur_injoignable(self.base_url, e))

        if not resp.ok:
            detail = _extraire_detail(resp)
            if resp.status_code == 401:
                # Un identifiant/mot de passe refusé à la connexion n'est pas
                # une session qui expire — il n'y a pas encore de session.
                # Le confondre avec une session expirée a fait croire à un
                # utilisateur que l'appli déconnait alors qu'il avait juste
                # mal tapé son mot de passe.
                if path == "/login":
                    raise ApiError(str(detail))
                # Un 401 « poste non autorisé » vient de la clé du poste, pas de
                # la session : déconnecter l'utilisateur ne réglerait rien et le
                # message serait trompeur.
                if "poste" in str(detail).lower():
                    raise ApiError(str(detail))
                self.user_token = ""
                self.username = ""
                raise SessionExpiredError("Session expirée — veuillez vous reconnecter")
            erreur = ApiError(str(detail))
            erreur.status_code = resp.status_code
            raise erreur
        return resp

    def get_ws_ticket(self) -> Optional[str]:
        """Billet à usage unique (30s) pour ouvrir le WebSocket sans exposer
        le token de session complet dans l'URL. None si l'appel échoue :
        l'appelant retombe alors sur le token complet (compatibilité)."""
        try:
            return self._request("POST", "/ws-ticket").json().get("ticket")
        except ApiError:
            return None

    def get_version_disponible(self) -> dict:
        """Dernière version publiée par l'admin, ou {} si l'appel échoue.

        Un serveur plus ancien sans cet endpoint (404) ne doit jamais empêcher
        de travailler : purement informatif, jamais une erreur affichée.
        """
        try:
            return self._request("GET", "/version").json()
        except ApiError:
            return {}

    def get_products(self, include_archived: bool = False,
                     all_sites: bool = False) -> list[dict]:
        """`all_sites=True` lève le filtre par site (secteur FUEL) — réservé
        à l'admin (ignoré sinon) : sert à l'écran Produits, qui doit gérer
        le catalogue de tous les sites, contrairement à
        Stock/Réception/Expédition/Jaugeage (scopés au site de l'appelant,
        même pour un admin)."""
        params = {}
        if include_archived:
            params["include_archived"] = "true"
        if all_sites:
            params["all_sites"] = "true"
        return self._request("GET", "/products", params=params or None).json()

    def create_product(self, sku: str, name: str, unit: str, current_stock: float, min_stock: float,
                        unit_type: str = "piece", bidon_capacity: Optional[float] = None,
                        unit_cost: Optional[float] = None,
                        barcode: Optional[str] = None) -> dict:
        payload = {
            "sku": sku,
            "name": name,
            "unit": unit,
            "unit_type": unit_type,
            "current_stock": current_stock,
            "min_stock": min_stock,
        }
        if bidon_capacity is not None:
            payload["bidon_capacity"] = bidon_capacity
        if unit_cost is not None:
            payload["unit_cost"] = unit_cost
        if barcode:
            payload["barcode"] = barcode
        return self._request("POST", "/products", json=payload).json()

    def scan_barcode(self, code: str) -> Optional[dict]:
        """Produit portant ce code-barres, ou None si aucun ne le porte.

        Un code inconnu est le cas NORMAL au démarrage (aucune fiche n'a de
        code) : renvoyer None évite à chaque écran d'entourer l'appel d'un
        try/except, et laisse l'écran proposer le rattachement. Les autres
        erreurs (serveur injoignable, session expirée) remontent telles quelles.
        """
        from urllib.parse import quote

        try:
            return self._request(
                "GET", f"/products/scan/{quote(str(code).strip(), safe='')}").json()
        except ApiError as e:
            if getattr(e, "status_code", None) == 404:
                return None
            raise

    def import_products(self, file_path: str) -> dict:
        with open(file_path, "rb") as f:
            files = {"file": (Path(file_path).name, f)}
            return self._request("POST", "/products/import", files=files).json()

    # ------------------------------------------------------------- photos
    #
    # Les images ne passent pas en JSON : envoi en multipart, réception en
    # octets bruts. Le serveur vérifie le format aux premiers octets et refuse
    # tout ce qui n'est pas JPG/PNG/WebP, quel que soit le nom du fichier.

    def upload_product_photo(self, product_id: int, file_path: str) -> dict:
        with open(file_path, "rb") as f:
            files = {"file": (Path(file_path).name, f)}
            return self._request(
                "POST", f"/products/{product_id}/photo", files=files).json()

    def get_product_photo(self, product_id: int) -> Optional[bytes]:
        """Octets de la photo, ou None si le produit n'en a pas.

        Une fiche sans photo est le cas courant, pas une erreur : renvoyer None
        évite à chaque écran d'entourer l'appel d'un try/except.
        """
        try:
            return self._request("GET", f"/products/{product_id}/photo").content
        except ApiError:
            return None

    def delete_product_photo(self, product_id: int) -> dict:
        return self._request("DELETE", f"/products/{product_id}/photo").json()

    def upload_document_photo(self, document_id: int, file_path: str,
                              caption: str = "") -> dict:
        params = {"caption": caption} if caption else None
        with open(file_path, "rb") as f:
            files = {"file": (Path(file_path).name, f)}
            return self._request("POST", f"/documents/{document_id}/photo",
                                 files=files, params=params).json()

    def get_document_photos(self, document_id: int) -> list[dict]:
        return self._request(
            "GET", f"/documents/{document_id}/photos").json().get("photos", [])

    def get_document_photo(self, document_id: int, photo_id: int) -> Optional[bytes]:
        try:
            return self._request(
                "GET", f"/documents/{document_id}/photos/{photo_id}").content
        except ApiError:
            return None

    def get_stock(self) -> list[dict]:
        return self._request("GET", "/stock").json()

    def get_stock_reconciliation(self) -> dict:
        return self._request("GET", "/stock/reconciliation").json()

    def get_historique_produit(self, product_id: int, limite: int = 50) -> dict:
        """Vie complète d'un produit : mouvements et changements de fiche.

        Une seule réponse déjà triée du plus récent au plus ancien — l'écran
        n'a rien à recouper.
        """
        return self._request("GET", f"/products/{product_id}/historique",
                             params={"limite": limite}).json()

    def get_reappro_report(self, mois_couverture: Optional[float] = None) -> list[dict]:
        """Produits sous leur seuil, avec la quantité à commander.

        `mois_couverture` (facultatif) demande une quantité calculée sur la
        consommation récente pour tenir ce nombre de mois, au lieu du strict
        minimum pour repasser le seuil. Omis = comportement historique.
        """
        return self._request("GET", "/reports/reappro",
                             params=self._params_couverture(mois_couverture)).json()

    @staticmethod
    def _params_couverture(mois_couverture: Optional[float]) -> dict:
        """Paramètres de requête du rapport réappro — vides si non demandé.

        Envoyer `mois_couverture=None` sérialiserait « None » dans l'URL et le
        serveur refuserait la valeur : le paramètre doit être absent.
        """
        return {} if mois_couverture is None else {"mois_couverture": mois_couverture}

    def get_ajustements_report(self, jours: int = 7) -> dict:
        return self._request("GET", "/reports/ajustements", params={"jours": jours}).json()

    def get_reference_regions(self, sector: str | None = None) -> dict:
        """Régions/superviseurs du secteur de l'appelant.

        `sector` n'est respecté par le serveur que pour un admin : sert au
        formulaire de création d'utilisateur, où un admin doit voir les
        régions du secteur choisi dans "Profil" (pas forcément le sien).
        """
        params = {"sector": sector} if sector else {}
        return self._request("GET", "/reference/regions", params=params).json()

    def get_reference_technicians(self) -> dict:
        return self._request("GET", "/reference/technicians").json()

    def get_reference_fuel(self) -> dict:
        """Liste des « projets » Fuel connus (suggestions, pas une liste fermée)."""
        return self._request("GET", "/reference/fuel").json()

    def get_fuel_vehicles(self) -> dict:
        """Plaques déjà utilisées par ce secteur (autocomplétion)."""
        return self._request("GET", "/fuel/vehicles").json()

    def get_niveau_cuve(self, product_id: int, jours: int = 30) -> dict:
        """Niveau d'une cuve dans le temps, pour la courbe du tableau de bord."""
        return self._request("GET", f"/fuel/niveau-cuve/{product_id}",
                             params={"jours": jours}).json()

    def get_dashboard(self) -> dict:
        return self._request("GET", "/dashboard").json()

    def get_dashboard_categorie(self, categorie: str) -> dict:
        return self._request("GET", f"/dashboard/categorie/{quote(categorie, safe='')}").json()

    def get_dashboard_cuve(self, product_id: int) -> dict:
        return self._request("GET", f"/dashboard/cuve/{product_id}").json()

    def export_stock(self, save_path: str) -> None:
        resp = self._request("GET", "/stock/export")
        Path(save_path).write_bytes(resp.content)

    def export_reappro_report(self, save_path: str,
                              mois_couverture: Optional[float] = None) -> None:
        resp = self._request("GET", "/reports/reappro/export",
                             params=self._params_couverture(mois_couverture))
        Path(save_path).write_bytes(resp.content)

    def export_inventaires_regionaux(self, save_path: str, region: Optional[str] = None,
                                     date_from: Optional[str] = None,
                                     date_to: Optional[str] = None) -> None:
        """Excel des comptages régionaux — MÊME période que la liste à l'écran.

        Les bornes sont facultatives et au format AAAA-MM-JJ, inclusives.
        """
        params: dict = {"region": region} if region else {}
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        resp = self._request("GET", "/regional/inventaire/export", params=params)
        Path(save_path).write_bytes(resp.content)

    def export_comptable(self, depuis: Optional[str] = None,
                          jusqu_a: Optional[str] = None) -> bytes:
        """Classeur comptable de la période (défaut serveur : 30 derniers jours).

        Renvoie les octets au lieu de les écrire, contrairement aux autres
        exports : l'appelant demande le chemin de sauvegarde APRÈS coup, de
        sorte qu'un refus du serveur (403, période invalide) n'ouvre pas une
        boîte « Enregistrer sous » pour rien. Dates au format AAAA-MM-JJ.
        """
        params = {}
        if depuis:
            params["depuis"] = depuis
        if jusqu_a:
            params["jusqu_a"] = jusqu_a
        return self._request("GET", "/reports/comptable/export", params=params).content

    def envoyer_rapport_mensuel(self) -> dict:
        """Demande au serveur d'envoyer le rapport de stock par e-mail.

        Réservé à l'administrateur. Le serveur répond 400 — remonté ici en
        `ApiError` avec son message — tant qu'aucun destinataire n'est
        configuré dans `email_config.json`.
        """
        return self._request("POST", "/reports/mensuel/envoyer-maintenant").json()

    def create_document(self, doc_type: str, party: str, operator: str, note: str, lines: list[dict],
                         region: str = "", reference: str = "", created_at: Optional[str] = None,
                         idempotency_key: Optional[str] = None,
                         created_by: str = "", carrier: Optional[str] = None,
                         party_contact_id: Optional[int] = None,
                         technician: Optional[str] = None,
                         vehicle_plate: Optional[str] = None,
                         vehicle_info: Optional[str] = None,
                         mileage: Optional[float] = None,
                         project: Optional[str] = None,
                         receiver: Optional[str] = None,
                         fuel_card: Optional[str] = None) -> dict:
        payload = {
            "created_by": created_by or None,
            "type": doc_type,
            "party": party or None,
            "party_contact_id": party_contact_id,
            "operator": operator or None,
            "region": region or None,
            "reference": reference or None,
            "note": note or None,
            "carrier": carrier or None,
            # Technicien nommé sur une expédition : suivi de consigne
            # bouteille par personne, en plus du suivi par région.
            "technician": technician or None,
            # Champs Fuel (secteur FUEL uniquement) : None partout ailleurs.
            "vehicle_plate": vehicle_plate or None,
            "vehicle_info": vehicle_info or None,
            "mileage": mileage,
            "project": project or None,
            "receiver": receiver or None,
            "fuel_card": fuel_card or None,
            "created_at": created_at or None,
            "idempotency_key": idempotency_key,
            "lines": lines,
        }
        resp = self._request("POST", "/documents", json=payload)
        data = resp.json()
        # Vrai si le serveur avait déjà enregistré ce bon (réponse perdue au
        # premier envoi) : aucun doublon n'a été créé.
        data["_rejeu"] = resp.headers.get("X-Idempotent-Replay") == "true"
        return data

    def create_adjustment(self, reason: str, lines: list[dict], operator: str = "",
                           region: str = "", note: str = "", created_at: Optional[str] = None,
                           idempotency_key: Optional[str] = None,
                           created_by: str = "",
                           confirmation_ecart_important: bool = False) -> dict:
        payload = {
            "created_by": created_by or None,
            "reason": reason,
            "operator": operator or None,
            "region": region or None,
            "note": note or None,
            "created_at": created_at or None,
            "idempotency_key": idempotency_key,
            "confirmation_ecart_important": confirmation_ecart_important,
            "lines": lines,
        }
        resp = self._request("POST", "/adjustments", json=payload)
        data = resp.json()
        data["_rejeu"] = resp.headers.get("X-Idempotent-Replay") == "true"
        return data

    def cancel_document(self, document_id: int, reason: str, operator: str = "") -> dict:
        payload = {"reason": reason, "operator": operator or None}
        return self._request("POST", f"/documents/{document_id}/cancel", json=payload).json()

    def list_documents(self, doc_type: Optional[str] = None, limit: int = 200,
                        offset: int = 0,
                        reference_prefix: Optional[str] = None,
                        date_from: Optional[str] = None,
                        date_to: Optional[str] = None,
                        product_id: Optional[int] = None,
                        search: Optional[str] = None,
                        operator: Optional[str] = None) -> tuple[list[dict], int]:
        """Renvoie (bons de la page, total disponible).

        Le total permet d'indiquer à l'opérateur qu'il ne voit qu'une partie
        des mouvements, au lieu de tronquer silencieusement.

        `search`/`operator` sont filtrés côté serveur (sur TOUS les
        documents, pas seulement la page courante) : sans ça, un document
        correspondant situé sur une autre page restait invisible.
        """
        params = {"limit": limit, "offset": offset}
        if doc_type:
            params["type"] = doc_type
        if reference_prefix:
            params["reference_prefix"] = reference_prefix
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        if product_id:
            params["product_id"] = product_id
        if search:
            params["search"] = search
        if operator:
            params["operator"] = operator
        resp = self._request("GET", "/documents", params=params)
        total = int(resp.headers.get("X-Total-Count", 0))
        return resp.json(), total

    def export_documents(self, save_path: str, doc_type: Optional[str] = None,
                         date_from: Optional[str] = None, date_to: Optional[str] = None,
                         product_id: Optional[int] = None,
                         reference_prefix: Optional[str] = None) -> dict:
        """Écrit le fichier et renvoie ce que le serveur dit de son contenu.

        `tronque` vaut True quand l'historique dépassait la limite d'export :
        le fichier ne contient alors que les bons les plus récents. L'appelant
        doit le dire à l'opérateur, sinon il croit tenir un export complet.
        """
        params = {}
        if doc_type:
            params["type"] = doc_type
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        if product_id:
            params["product_id"] = product_id
        if reference_prefix:
            params["reference_prefix"] = reference_prefix
        resp = self._request("GET", "/documents/export", params=params)
        Path(save_path).write_bytes(resp.content)

        def _entier(nom: str) -> int:
            try:
                return int(resp.headers.get(nom, 0))
            except (TypeError, ValueError):
                return 0

        return {
            "total": _entier("X-Total-Count"),
            "exportes": _entier("X-Returned-Count"),
            "tronque": resp.headers.get("X-Export-Truncated") == "1",
        }

    def users_status(self) -> dict:
        return self._request("GET", "/users/status").json()

    def login(self, username: str, password: str) -> dict:
        return self._request("POST", "/login", json={
            "username": username, "password": password,
        }).json()

    def logout(self) -> None:
        try:
            self._request("POST", "/logout")
        except ApiError:
            pass
        self.user_token = ""
        self.username = ""

    def get_users(self, sector: str | None = None) -> list[dict]:
        """`sector="ALL"` lève le filtre par défaut (son propre secteur) —
        réservé à l'admin qui amorce les comptes d'un autre secteur."""
        params = {"sector": sector} if sector else {}
        return self._request("GET", "/users", params=params).json()

    def create_user(self, username: str, password: str, display_name: str,
                    role: str = "magasinier", region: Optional[str] = None,
                    sector: str = "CONSUMABLES", site: Optional[str] = None) -> dict:
        payload = {"username": username, "password": password,
                   "display_name": display_name, "role": role, "sector": sector}
        if region:
            payload["region"] = region
        if site:
            payload["site"] = site
        return self._request("POST", "/users", json=payload).json()

    def update_user(self, user_id: int, **kwargs) -> dict:
        return self._request("PUT", f"/users/{user_id}", json=kwargs).json()

    def get_login_history(self) -> list[dict]:
        return self._request("GET", "/login-history").json()

    def get_revue_comptes(self) -> dict:
        """Comptes et ancienneté de leur dernière connexion (admin)."""
        return self._request("GET", "/users/revue").json()

    def get_regions_utilisateur(self, user_id: int) -> dict:
        return self._request("GET", f"/users/{user_id}/regions").json()

    def set_regions_utilisateur(self, user_id: int, regions: list[str]) -> dict:
        """Restreint un lecteur à des régions. Liste vide = il revoit tout."""
        return self._request("PUT", f"/users/{user_id}/regions",
                             json={"regions": regions}).json()

    # ------------------------------------------- référentiel et annotations

    def get_qualite_referentiel(self) -> dict:
        """Fiches produit présentant un défaut de saisie (admin)."""
        return self._request("GET", "/products/qualite").json()

    # ------------------------------------------- indicateurs de pilotage
    # Tous réservés à l'administrateur côté serveur : l'écran Indicateurs
    # n'apparaît que pour ce rôle.

    def get_precision_inventaire(self, jours_cycle: int = 7) -> dict:
        return self._request("GET", "/reports/precision-inventaire",
                             params={"jours_cycle": jours_cycle}).json()

    def get_concentration_regions(self, jours: int = 90) -> dict:
        return self._request("GET", "/reports/concentration-regions",
                             params={"jours": jours}).json()

    def get_comptages_a_temps(self) -> dict:
        return self._request("GET", "/reports/comptages-a-temps").json()

    def get_anomalies_mouvements(self, facteur: float = 3.0,
                                 limite: int = 10) -> dict:
        return self._request("GET", "/reports/anomalies",
                             params={"facteur": facteur, "limite": limite}).json()

    def get_consommation_regionale(self, mois: int = 12) -> dict:
        return self._request("GET", "/reports/consommation-regionale",
                             params={"mois": mois}).json()

    def get_produits_dormants(self, jours: int = 180) -> dict:
        return self._request("GET", "/reports/produits-dormants",
                             params={"jours": jours}).json()

    def get_resolution_alertes(self, jours: int = 90) -> dict:
        return self._request("GET", "/reports/resolution-alertes",
                             params={"jours": jours}).json()

    def get_dette_consigne(self) -> dict:
        return self._request("GET", "/reports/dette-consigne").json()

    def get_activite_operateurs(self, jours: int = 30) -> dict:
        return self._request("GET", "/reports/activite-operateurs",
                             params={"jours": jours}).json()

    def get_ecarts_reception(self, jours: int = 90) -> dict:
        """Écarts expédié / reçu cumulés par région et transporteur (admin)."""
        return self._request("GET", "/reports/ecarts-reception",
                             params={"jours": jours}).json()

    def get_delai_fournisseurs(self, jours: int = 365,
                               min_receptions: int = 2) -> dict:
        """Régularité d'approvisionnement par fournisseur (admin).

        Ce n'est PAS un délai commande -> livraison : le serveur n'a aucune
        date de commande en base. La mesure est l'intervalle entre deux
        réceptions successives d'un même fournisseur.
        """
        return self._request("GET", "/reports/delai-fournisseurs",
                             params={"jours": jours,
                                     "min_receptions": min_receptions}).json()

    def get_tableau_de_bord_regions(self) -> dict:
        """État consolidé de chaque région à entrepôt, en un seul appel (admin).

        Renvoie {"regions": [{region, stock_declare, alertes_ouvertes,
        dernier_comptage, jours_depuis_comptage, comptage_perime, note,
        transferts_en_transit}], ...}.
        """
        return self._request("GET", "/regions/tableau-de-bord").json()

    def get_notes_regions(self) -> dict:
        return self._request("GET", "/regions/notes").json()

    def set_note_region(self, region: str, note: str) -> dict:
        return self._request("PUT", "/regions/note",
                             json={"region": region, "note": note}).json()

    # ------------------------------------------- entrepôts régionaux
    # Un compte régional n'a accès qu'à ces appels ; le serveur refuse tout le
    # reste. Le paramètre `region` n'est utile qu'à un administrateur central :
    # pour un compte régional, le serveur impose toujours sa propre région.

    def get_transferts_en_transit(self, region: Optional[str] = None) -> dict:
        params = {"region": region} if region else {}
        return self._request("GET", "/regional/transferts-en-transit",
                             params=params).json()

    def confirmer_reception(self, document_id: int, lines: list[dict],
                            note: str = "") -> dict:
        """lines : [{"product_id": int, "received_quantity": float}, ...]

        Une ligne omise vaut « reçue conforme ».
        """
        payload = {"lines": lines}
        if note:
            payload["note"] = note
        return self._request("POST", f"/documents/{document_id}/confirmer-reception",
                             json=payload).json()

    def get_transferts_sortants(self, region: Optional[str] = None) -> dict:
        """Transferts partis de cette région, pas encore confirmés à l'arrivée."""
        params = {"region": region} if region else {}
        return self._request("GET", "/regional/transferts-sortants",
                             params=params).json()

    def creer_transfert_regional(self, region_destination: str, lines: list[dict],
                                 region_source: Optional[str] = None,
                                 note: str = "", reference: str = "",
                                 carrier: str = "",
                                 created_at: Optional[str] = None,
                                 idempotency_key: Optional[str] = None) -> dict:
        """Envoi d'un entrepôt régional vers un autre.

        lines : [{"product_id": int, "quantity": float}, ...]

        `region_source` est ignorée par le serveur pour un compte régional :
        sa propre région fait toujours foi.

        `idempotency_key` : même protection que sur les bons et l'inventaire.
        Sans elle, un transfert rejoué depuis la file d'attente hors ligne
        (réponse perdue après une coupure) créait un SECOND transfert et
        débitait deux fois l'entrepôt régional d'origine.
        """
        payload: dict = {"region_destination": region_destination, "lines": lines}
        if region_source:
            payload["region_source"] = region_source
        if note:
            payload["note"] = note
        if reference:
            payload["reference"] = reference
        if carrier:
            payload["carrier"] = carrier
        if created_at:
            payload["created_at"] = created_at
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return self._request("POST", "/regional/transferts", json=payload).json()

    def get_stock_regional(self, region: Optional[str] = None,
                           inclure_catalogue: bool = False) -> dict:
        """Stock détenu par l'entrepôt régional.

        `inclure_catalogue` ajoute à quantité 0 les articles actifs que la
        région ne détient pas : l'écran de transfert les montre, pour qu'on
        voie ce qui manque au lieu de chercher un article absent de la liste.
        """
        params: dict = {"region": region} if region else {}
        if inclure_catalogue:
            params["inclure_catalogue"] = "true"
        return self._request("GET", "/regional/stock", params=params).json()

    def get_catalogue_regional(self) -> dict:
        """Catalogue minimal (id/sku/name/unit) accessible à un compte régional."""
        return self._request("GET", "/regional/catalogue").json()

    def get_toutes_alertes_regionales(self) -> dict:
        """Total des ruptures signalées, toutes régions confondues (admin)."""
        return self._request("GET", "/regional/alertes/toutes").json()

    def get_alertes_regionales(self, region: Optional[str] = None,
                               historique: bool = False) -> dict:
        """Signalements de rupture pour la région (ouverts par défaut).

        Renvoie {"region", "alertes": [{signal_id, product_id, sku, name,
        unit, note, created_by, created_at, resolved_at, resolved_by}], "nombre"}.

        `historique=True` : inclut aussi les signalements déjà résolus.
        """
        params = {}
        if region:
            params["region"] = region
        if historique:
            params["historique"] = True
        return self._request("GET", "/regional/alertes", params=params).json()

    def signaler_alerte_regionale(self, product_id: int, note: str = "",
                                  region: Optional[str] = None) -> dict:
        """Signale un produit bas / en rupture. 409 si déjà signalé et non résolu."""
        payload: dict = {"product_id": product_id}
        if note:
            payload["note"] = note
        if region:
            payload["region"] = region
        return self._request("POST", "/regional/alertes/signaler",
                             json=payload).json()

    def resoudre_alerte_regionale(self, signal_id: int) -> dict:
        """Marque un signalement résolu. Réservé à un administrateur."""
        return self._request("POST",
                             f"/regional/alertes/{signal_id}/resoudre").json()

    def creer_inventaire_regional(self, lines: list[dict], region: Optional[str] = None,
                                  note: str = "", created_at: Optional[str] = None,
                                  idempotency_key: Optional[str] = None) -> dict:
        """lines : [{"product_id": int, "quantity": float}, ...]

        `created_at` : date du comptage PHYSIQUE, à poser par l'appelant avant
        un éventuel passage par la file d'attente hors ligne — sinon un
        comptage rejoué plus tard s'affiche daté du jour de la reprise réseau.

        `idempotency_key` : même protection que sur les bons et les transferts
        régionaux. Sans elle, un comptage rejoué depuis la file d'attente hors
        ligne (réponse perdue après une coupure) créait un second rapport,
        doublant le total agrégé du dernier comptage.
        """
        payload: dict = {"lines": lines}
        if region:
            payload["region"] = region
        if note:
            payload["note"] = note
        if created_at:
            payload["created_at"] = created_at
        if idempotency_key:
            payload["idempotency_key"] = idempotency_key
        return self._request("POST", "/regional/inventaire", json=payload).json()

    def get_documents_regionaux(self, region: Optional[str] = None,
                                doc_type: Optional[str] = None,
                                limit: int = 200) -> list[dict]:
        """Bons touchant la région — reçus ET envoyés — pour l'écran "Mon
        historique" d'un compte régional.

        Distinct de `list_documents` : celui-ci vise `/documents`, route
        fermée à un compte régional par le serveur (il ne doit voir ni le
        catalogue central ni l'historique d'une autre région). Cette méthode
        vise `/regional/documents`, bornée par le serveur à la propre région
        de l'appelant.
        """
        params: dict = {"limit": limit}
        if region:
            params["region"] = region
        if doc_type:
            params["type"] = doc_type
        return self._request("GET", "/regional/documents", params=params).json()

    def get_inventaires_regionaux(self, region: Optional[str] = None,
                                  limit: int = 200,
                                  date_from: Optional[str] = None,
                                  date_to: Optional[str] = None) -> dict:
        """Historique des comptages régionaux.

        `date_from` / `date_to` : bornes facultatives AAAA-MM-JJ, inclusives.
        """
        params: dict = {"limit": limit}
        if region:
            params["region"] = region
        if date_from:
            params["date_from"] = date_from
        if date_to:
            params["date_to"] = date_to
        return self._request("GET", "/regional/inventaire", params=params).json()

    def update_product(self, product_id: int, **kwargs) -> dict:
        return self._request("PATCH", f"/products/{product_id}", json=kwargs).json()

    def set_global_stock_date(self, date: str, reset_initial: bool = False) -> dict:
        return self._request("PATCH", "/products/stock-date",
                             json={"date": date, "reset_initial": reset_initial}).json()

    def update_thresholds(self, thresholds: list[dict]) -> dict:
        """Met a jour les seuils min_stock en batch.

        thresholds: [{"product_id": int, "min_stock": float}, ...]
        """
        return self._request("PATCH", "/products/thresholds", json=thresholds).json()

    def change_my_password(self, old_password: str, new_password: str) -> dict:
        return self._request("PATCH", "/me/password", json={
            "old_password": old_password, "new_password": new_password,
        }).json()

    def get_me(self) -> dict:
        return self._request("GET", "/me").json()

    def get_contacts(self, contact_type: Optional[str] = None,
                     include_inactive: bool = False) -> list[dict]:
        params = {}
        if contact_type:
            params["type"] = contact_type
        if include_inactive:
            params["include_inactive"] = "true"
        return self._request("GET", "/contacts", params=params).json()

    def create_contact(self, name: str, contact_type: str = "other", **kwargs) -> dict:
        payload = {"name": name, "type": contact_type, **kwargs}
        return self._request("POST", "/contacts", json=payload).json()

    def update_contact(self, contact_id: int, **kwargs) -> dict:
        return self._request("PUT", f"/contacts/{contact_id}", json=kwargs).json()

    def get_contact_documents(self, contact_id: int, limit: int = 50) -> list[dict]:
        return self._request("GET", f"/contacts/{contact_id}/documents",
                             params={"limit": limit}).json()

    def create_return(self, party: str, operator: str, note: str, lines: list[dict],
                      region: str = "", reference: str = "", created_at: Optional[str] = None,
                      idempotency_key: Optional[str] = None,
                      created_by: str = "", carrier: Optional[str] = None,
                      party_contact_id: Optional[int] = None,
                      vehicle_plate: Optional[str] = None,
                      vehicle_info: Optional[str] = None,
                      mileage: Optional[float] = None,
                      project: Optional[str] = None,
                      receiver: Optional[str] = None,
                      fuel_card: Optional[str] = None) -> dict:
        payload = {
            "created_by": created_by or None,
            "type": "RETURN",
            "party": party or None,
            "party_contact_id": party_contact_id,
            "operator": operator or None,
            "region": region or None,
            "reference": reference or None,
            "note": note or None,
            "carrier": carrier or None,
            "vehicle_plate": vehicle_plate or None,
            "vehicle_info": vehicle_info or None,
            "mileage": mileage,
            "project": project or None,
            "receiver": receiver or None,
            "fuel_card": fuel_card or None,
            "created_at": created_at or None,
            "idempotency_key": idempotency_key,
            "lines": lines,
        }
        resp = self._request("POST", "/returns", json=payload)
        data = resp.json()
        data["_rejeu"] = resp.headers.get("X-Idempotent-Replay") == "true"
        return data

    def get_bottle_balance(self, par_technicien: bool = False) -> list[dict]:
        """Soldes de bouteilles vides dues, par produit et par région.

        `par_technicien` bascule le regroupement sur le technicien nommé :
        c'est la vue « qui doit encore rendre ses bouteilles ».
        """
        params = {"par_technicien": "true"} if par_technicien else {}
        return self._request("GET", "/bottles/balance", params=params).json()

    def get_bottle_ledger(self, product_id: int | None = None,
                          region: str | None = None) -> list[dict]:
        """Liste les écritures de retour de bouteilles."""
        params = {}
        if product_id is not None:
            params["product_id"] = product_id
        if region:
            params["region"] = region
        return self._request("GET", "/bottles/ledger", params=params).json()

    def create_bottle_return(self, product_id: int, region: str, bottles: float,
                             note: str = "", created_by: str = "",
                             technician: str = "", reference: str = "") -> dict:
        """Enregistre le retour physique de bouteilles vides (ne touche pas au stock)."""
        payload = {
            "product_id": product_id,
            "region": region,
            "bottles": bottles,
            "note": note or None,
            "created_by": created_by or None,
            "technician": technician or None,
            # N° de la fiche papier signée à la reprise des bouteilles.
            "reference": reference or None,
        }
        return self._request("POST", "/bottles/return", json=payload).json()

    def cancel_bottle_return(self, entry_id: int) -> dict:
        """Annule un retour de bouteilles (admin uniquement)."""
        return self._request("POST", f"/bottles/return/{entry_id}/cancel").json()

    def get_rl_balance(self, par_technicien: bool = False) -> list[dict]:
        """Soldes d'équipement RL dû (livré, pas encore retourné), par produit/région."""
        params = {"par_technicien": "true"} if par_technicien else {}
        return self._request("GET", "/rl/balance", params=params).json()

    def get_rl_ledger(self, product_id: int | None = None,
                      region: str | None = None) -> list[dict]:
        """Liste les écritures de retour RL."""
        params = {}
        if product_id is not None:
            params["product_id"] = product_id
        if region:
            params["region"] = region
        return self._request("GET", "/rl/ledger", params=params).json()

    def create_rl_return(self, product_id: int, region: str, quantity: float,
                         note: str = "", created_by: str = "",
                         technician: str = "", reference: str = "") -> dict:
        """Enregistre le retour physique d'un équipement RL (ne touche pas au stock)."""
        payload = {
            "product_id": product_id,
            "region": region,
            "quantity": quantity,
            "note": note or None,
            "created_by": created_by or None,
            "technician": technician or None,
            "reference": reference or None,
        }
        return self._request("POST", "/rl/return", json=payload).json()

    def cancel_rl_return(self, entry_id: int) -> dict:
        """Annule un retour RL (admin uniquement)."""
        return self._request("POST", f"/rl/return/{entry_id}/cancel").json()

    def archive_product(self, product_id: int) -> dict:
        return self._request("DELETE", f"/products/{product_id}").json()

    def unarchive_product(self, product_id: int) -> dict:
        """Remet un produit archivé dans le catalogue actif.

        Le SKU restant réservé par l'index unique, c'est le seul moyen de
        récupérer un produit archivé par erreur.
        """
        return self._request("PATCH", f"/products/{product_id}",
                             json={"archived": 0}).json()

    def get_audit_log(self, limit: int = 100) -> list[dict]:
        return self._request("GET", "/audit-log", params={"limit": limit}).json()

    def export_audit_log(self, depuis: Optional[str] = None,
                         jusqu_a: Optional[str] = None,
                         entity_type: Optional[str] = None) -> bytes:
        """Journal d'audit de la période en classeur Excel (défaut : 30 jours).

        Renvoie les octets, comme `export_comptable` et pour la même raison :
        l'écran ne demande où enregistrer qu'une fois le fichier reçu, de
        sorte qu'un refus du serveur n'ouvre pas une boîte « Enregistrer
        sous » pour rien. Dates au format AAAA-MM-JJ.
        """
        params = {}
        if depuis:
            params["depuis"] = depuis
        if jusqu_a:
            params["jusqu_a"] = jusqu_a
        if entity_type:
            params["entity_type"] = entity_type
        return self._request("GET", "/audit-log/export", params=params).content

    def get_backup_statut(self) -> dict:
        """État des sauvegardes (admin), ou {} si l'appel échoue.

        Silencieux comme `get_version_disponible` : un serveur plus ancien
        sans cet endpoint, ou un compte non admin, ne doit jamais produire
        d'erreur à l'écran — l'information est utile, pas vitale.
        """
        try:
            return self._request("GET", "/backup/statut").json()
        except ApiError:
            return {}

    def get_etat_systeme(self) -> dict:
        """Santé du serveur en un appel (admin), ou {} si l'appel échoue.

        Volontairement silencieux, comme `get_backup_statut` : un serveur plus
        ancien sans l'endpoint renvoie 404, et l'écran d'état affiche alors
        « inconnu » au lieu d'une erreur technique incompréhensible pour un
        magasinier.
        """
        try:
            return self._request("GET", "/systeme/etat").json()
        except ApiError:
            return {}

    def get_regions_silence(self) -> dict:
        """Dernière activité connue de chaque région (admin), ou {} si échec."""
        try:
            return self._request("GET", "/regions/silence").json()
        except ApiError:
            return {}
