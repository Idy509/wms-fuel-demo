"""
Gestionnaire de connexions WebSocket pour le système de gestion d'entrepôt.
Permet la diffusion en temps réel des mises à jour aux clients connectés.
"""
from typing import Set, Dict, Optional, List
import json
import asyncio
from fastapi import WebSocket
from journalisation import logger

log = logger(__name__)


class ConnectionManager:
    def __init__(self):
        # Stocke les connexions avec leurs informations utilisateur associées
        self.active_connections: Dict[WebSocket, Optional[dict]] = {}
        # Track connection counts per IP for DoS protection
        self.connections_per_ip: Dict[str, int] = {}
        # Track last ping time for each connection for dead connection detection
        self.last_ping: Dict[WebSocket, float] = {}
        # Boucle d'événements du serveur. Les endpoints d'écriture sont
        # synchrones (SQLite bloquant) : ils tournent dans un thread de
        # travail, sans boucle asyncio. Pour diffuser depuis ces threads il
        # faut réinjecter la coroutine dans la boucle principale.
        self.loop: Optional[asyncio.AbstractEventLoop] = None

    async def connect(self, websocket: WebSocket, user: Optional[dict] = None):
        """Accepter une nouvelle connexion WebSocket avec protection DoS basique."""
        client_ip = websocket.client.host if websocket.client else "unknown"

        # Basic DoS protection: limit connections per IP
        if self.connections_per_ip.get(client_ip, 0) >= 10:  # Max 10 connections per IP
            await websocket.close(code=4008, reason="Trop de connexions depuis cette IP")
            return False

        await websocket.accept()
        self.active_connections[websocket] = user
        self.connections_per_ip[client_ip] = self.connections_per_ip.get(client_ip, 0) + 1
        self.last_ping[websocket] = asyncio.get_event_loop().time()
        user_info = f" (user: {user.get('username', 'unknown')})" if user else " (no user)"
        log.info(f"Nouvelle connexion WebSocket établie{user_info} depuis {client_ip}. Total: {len(self.active_connections)}")
        return True

    def disconnect(self, websocket: WebSocket):
        """Retirer une connexion WebSocket et nettoyer les tracking.

        Idempotent : `broadcast()` retire déjà une connexion dont l'envoi
        échoue, et `/ws` rappelle `disconnect` dans son `finally`. Le test
        d'appartenance à `active_connections` garantit qu'un second appel ne
        décrémente pas une deuxième fois le compteur par adresse.
        """
        if websocket not in self.active_connections:
            return
        client_ip = websocket.client.host if websocket.client else "unknown"
        del self.active_connections[websocket]
        restant = self.connections_per_ip.get(client_ip, 1) - 1
        if restant > 0:
            self.connections_per_ip[client_ip] = restant
        else:
            # L'entrée retombée à zéro était CONSERVÉE : le dictionnaire
            # gagnait une clé par adresse ayant ouvert un WebSocket et ne la
            # rendait jamais. Sans conséquence sur un LAN de deux postes, mais
            # sans borne non plus dès qu'un balayage ou une plage IPv6 s'y
            # présente — même défaut que celui corrigé sur le rate limiter (N7).
            self.connections_per_ip.pop(client_ip, None)
        self.last_ping.pop(websocket, None)
        log.info(f"Connexion WebSocket fermée. Total restant: {len(self.active_connections)}")

    def marquer_actif(self, websocket: WebSocket) -> None:
        """Date la dernière activité observée sur une connexion.

        Purement informatif (diagnostic d'une socket silencieuse) : la
        détection des connexions mortes est assurée par uvicorn, qui envoie
        ses propres ping/pong protocolaires (ws_ping_interval /
        ws_ping_timeout) et ferme la socket quand le pong n'arrive pas.
        """
        if websocket in self.active_connections:
            self.last_ping[websocket] = asyncio.get_event_loop().time()

    async def send_personal_message(self, message: dict, websocket: WebSocket):
        """Envoyer un message à une connexion spécifique avec validation de taille."""
        try:
            # Validate message size to prevent DoS
            message_str = json.dumps(message)
            if len(message_str) > 65536:  # 64KB max message size
                raise ValueError("Message too large")
            await websocket.send_text(message_str)
            # `marquer_actif` et non une écriture directe : la connexion peut
            # avoir été retirée pendant l'`await`, et redater une socket
            # disparue laissait une entrée orpheline dans `last_ping`.
            self.marquer_actif(websocket)
        except Exception as e:
            log.error(f"Erreur lors de l'envoi d'un message personnel: {e}")
            self.disconnect(websocket)

    async def broadcast(self, message: dict, user_filter: Optional[dict] = None,
                        role_filter: Optional[str] = None,
                        sector_filter: Optional[str] = None):
        """
        Envoyer un message aux connexions actives, filtré par secteur, utilisateur ou rôle.

        Args:
            message: Dictionnaire à sérialiser en JSON et diffuser
            user_filter: Si fourni, ne diffuser qu'aux connexions correspondant à cet utilisateur
                        (comparaison basée sur l'username pour simplicité)
            role_filter: Si fourni, ne diffuser qu'aux connexions ayant ce rôle
            sector_filter: Secteur (Consumables, FON, FUEL...) auquel l'événement
                        appartient. SEULES les connexions de ce secteur sont
                        servies.

        Cloisonnement par secteur, par défaut FERMÉ : sans `sector_filter` (et
        sans `user_filter` désignant nommément un destinataire), la diffusion
        n'est envoyée à personne. Un compte Consumables recevait autrement, en
        temps réel, les bons FON/FUEL (référence, région, tiers), les
        mouvements de stock et les seuils de TOUS les secteurs — ce que le
        HTTP lui refuse déjà (`_secteur_du_compte` scope chaque endpoint).
        Un oubli de secteur sur un nouvel appelant ne fuite donc rien : il
        rend la diffusion muette, ce qui se voit et se corrige.
        """
        if not self.active_connections:
            return

        if sector_filter is None and user_filter is None:
            log.warning(
                "diffusion WebSocket ignorée (aucun secteur précisé) : type=%s",
                message.get("type"))
            return

        message_str = json.dumps(message)
        # Validate message size to prevent DoS
        if len(message_str) > 65536:  # 64KB max message size
            log.warning(f"Tentative de diffusion de message trop large: {len(message_str)} bytes")
            return

        # Filtrer les connexions selon les critères fournis
        connections_to_notify = []
        # Instantané : la boucle ne contient aucun `await`, donc rien ne peut
        # modifier le registre pendant l'itération — mais la liste des envois
        # est ensuite jouée par `gather`, et `disconnect` est appelé juste
        # après sur les connexions en échec. Figer la vue ici garde la
        # correspondance index/connexion vraie de bout en bout.
        for websocket, user in list(self.active_connections.items()):
            # Un compte régional ne reçoit QUE ce qui le vise explicitement.
            # Le middleware `restreindre_comptes_regionaux` lui ferme le stock
            # central, le catalogue global et l'historique des autres régions
            # côté HTTP ; sans ce garde-fou, le WebSocket les lui livrait quand
            # même (stock_update, document_created d'une autre région,
            # thresholds_updated...). Même règle qu'en HTTP, au même endroit
            # pour tout le monde, plutôt qu'un role_filter à ne pas oublier sur
            # chaque diffusion.
            if (user or {}).get("role") == "regional" and role_filter != "regional":
                continue

            # Cloisonnement par secteur : une connexion ne reçoit que les
            # événements de SON secteur. Une connexion sans secteur connu
            # (anonyme) n'en reçoit aucun — même règle de défaut fermé que
            # ci-dessus.
            if sector_filter is not None:
                if (user or {}).get("sector") != sector_filter:
                    continue

            # Plus aucun autre filtre : le secteur a déjà tranché.
            if user_filter is None and role_filter is None:
                connections_to_notify.append(websocket)
                continue

            # Vérifier le filtre utilisateur
            user_match = True
            if user_filter is not None:
                if not user or user.get("username") != user_filter.get("username"):
                    user_match = False

            # Vérifier le filtre de rôle
            role_match = True
            if role_filter is not None:
                if not user or user.get("role") != role_filter:
                    role_match = False

            # Notifier si les deux filtres correspondent (ou si aucun filtre n'est fourni pour ce critère)
            if user_match and role_match:
                connections_to_notify.append(websocket)

        if not connections_to_notify:
            return

        # Utiliser asyncio.gather pour envoyer en parallèle
        # return_exceptions=True permet de continuer même si certaines connexions échouent
        results = await asyncio.gather(
            *[connection.send_text(message_str) for connection in connections_to_notify],
            return_exceptions=True
        )

        # Gérer les déconnexions lors de la diffusion
        for i, result in enumerate(results):
            if i >= len(connections_to_notify):
                continue
            if isinstance(result, Exception):
                log.warning(f"Échec d'envoi à une connexion WebSocket: {result}")
                # Récupérer la connexion correspondante pour la retirer
                self.disconnect(connections_to_notify[i])
            else:
                # Envoi réussi : la connexion est vivante, on la redate.
                self.marquer_actif(connections_to_notify[i])

# Toutes les diffusions partagent la même enveloppe : {"type": ..., "data": {...}}
# Le client (client/websocket_client.py) lit systématiquement la clé "data" ;
# une clé spécifique par type ("document", "product", ...) lui arrivait vide.
async def broadcast_stock_update(product_id: int, new_stock: float, timestamp: str = None, user: Optional[dict] = None, role_filter: Optional[str] = None,
                                 sector: Optional[str] = None):
    """Diffuser une mise à jour de stock."""
    if timestamp is None:
        from horodatage import maintenant_texte
        timestamp = maintenant_texte()

    await manager.broadcast({
        "type": "stock_update",
        "data": {
            "product_id": product_id,
            "new_stock": new_stock,
            "timestamp": timestamp,
        },
    }, user_filter=user, role_filter=role_filter, sector_filter=sector)


async def broadcast_document_created(document_data: dict, user: Optional[dict] = None, role_filter: Optional[str] = None,
                                     sector: Optional[str] = None):
    """Diffuser la création d'un nouveau document."""
    await manager.broadcast({
        "type": "document_created",
        "data": document_data,
    }, user_filter=user, role_filter=role_filter, sector_filter=sector)


async def broadcast_document_cancelled(document_id: int, reason: str = None,
                                       doc_type: Optional[str] = None,
                                       user: Optional[dict] = None, role_filter: Optional[str] = None,
                                       sector: Optional[str] = None):
    """Diffuser l'annulation d'un document."""
    await manager.broadcast({
        "type": "document_cancelled",
        "data": {
            "document_id": document_id,
            "doc_type": doc_type,
            "reason": reason,
        },
    }, user_filter=user, role_filter=role_filter, sector_filter=sector)


async def broadcast_product_updated(product_data: dict, user: Optional[dict] = None, role_filter: Optional[str] = None,
                                    sector: Optional[str] = None):
    """Diffuser la mise à jour d'un produit."""
    await manager.broadcast({
        "type": "product_updated",
        "data": product_data,
    }, user_filter=user, role_filter=role_filter, sector_filter=sector)


async def broadcast_alert_triggered(alert_data: dict, user: Optional[dict] = None, role_filter: Optional[str] = None,
                                    sector: Optional[str] = None):
    """Diffuser une alerte déclenchée."""
    await manager.broadcast({
        "type": "alert_triggered",
        "data": alert_data,
    }, user_filter=user, role_filter=role_filter, sector_filter=sector)


async def broadcast_regional_alert_signaled(signal_data: dict, user: Optional[dict] = None, role_filter: Optional[str] = None,
                                            sector: Optional[str] = None):
    """Diffuse le signalement manuel d'une rupture par une région."""
    await manager.broadcast({
        "type": "regional_alert_signaled",
        "data": signal_data,
    }, user_filter=user, role_filter=role_filter, sector_filter=sector)


async def broadcast_thresholds_updated(thresholds_data: dict, user: Optional[dict] = None, role_filter: Optional[str] = None,
                                       sector: Optional[str] = None):
    """Diffuser la mise à jour des seuils de réapprovisionnement."""
    await manager.broadcast({
        "type": "thresholds_updated",
        "data": thresholds_data,
    }, user_filter=user, role_filter=role_filter, sector_filter=sector)


# Instance globale du gestionnaire de connexions WebSocket
manager = ConnectionManager()


def enregistrer_boucle(loop: asyncio.AbstractEventLoop) -> None:
    """Mémorise la boucle d'événements du serveur (appelé au démarrage)."""
    manager.loop = loop


def planifier(coro) -> None:
    """Diffuse une coroutine depuis n'importe quel thread, sans bloquer.

    Les endpoints d'écriture sont synchrones : FastAPI les exécute dans un
    thread de travail où `await` est impossible. On réinjecte donc la
    coroutine dans la boucle principale, qui la jouera dès qu'elle reprend
    la main. Aucune diffusion ne doit jamais faire échouer une écriture :
    toute erreur est journalisée, pas propagée.
    """
    loop = manager.loop
    if loop is None or not manager.active_connections:
        # Personne à prévenir : on ferme la coroutine pour éviter le
        # warning "coroutine was never awaited".
        coro.close()
        return
    try:
        future = asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception as e:  # boucle fermée, serveur en cours d'arrêt
        log.warning(f"Diffusion WebSocket ignorée : {e}")
        coro.close()
        return

    def _journaliser(f):
        try:
            f.result()
        except Exception as e:
            log.warning(f"Échec de diffusion WebSocket : {e}")

    future.add_done_callback(_journaliser)