"""
Client WebSocket pour recevoir les mises à jour en temps réel du serveur.
"""
import json
import threading

# Import local - sera résolu quand ce module sera utilisé
try:
    # `journalisation.logger` est une FABRIQUE (elle prend un nom et rend un
    # Logger), pas un Logger. L'ancienne ligne la liait telle quelle à
    # `logger` : le moindre `logger.info(...)` de ce fichier levait alors
    # AttributeError. Dans `_on_close`, cela sautait `_programmer_reconnexion`
    # et tuait toute reconnexion WebSocket, silencieusement. Le module
    # n'existant aujourd'hui que côté serveur, seul le repli s'exécutait en
    # production et le défaut restait invisible — mais il n'attendait qu'un
    # `journalisation.py` livré au client pour se déclencher.
    from journalisation import logger as _fabrique_logger

    logger = _fabrique_logger(__name__)
except ImportError:
    # Repli si le module de journalisation n'est pas disponible (cas du client)
    import logging
    logger = logging.getLogger(__name__)


class WMSWebSocketClient:
    def __init__(self, host, port, on_message_callback=None, token=None, ticket_provider=None,
                 on_giveup_callback=None, ws_url=None,
                 on_disconnect_callback=None, on_reconnect_callback=None):
        """
        Initialise le client WebSocket.

        Args:
            host: Adresse IP du serveur
            port: Port du serveur
            on_message_callback: Fonction appelée lorsqu'un message est reçu
                                Signature: callback(message_type, data)
            token: Token d'authentification pour la connexion WebSocket (repli
                   si aucun billet n'est disponible — voir `ticket_provider`)
            ticket_provider: Callable sans argument renvoyant un billet à usage
                   unique (30s, via POST /ws-ticket) ou None. Appelé à chaque
                   tentative de connexion — y compris les reconnexions, car un
                   billet déjà utilisé ne l'est plus. Préféré à `token` : il
                   évite que le token de session complet (8h) transite en
                   clair dans l'URL du WebSocket.
            on_giveup_callback: Appelé (sans argument) quand la reconnexion est
                   abandonnée — après max_reconnect_attempts échecs, ou si le
                   module websocket-client est absent. Sans lui, l'opérateur
                   ne voit jamais que le temps réel s'est arrêté : seul le
                   polling de secours (30s) continue, en silence.
            ws_url: URL complète du WebSocket (« wss://wms.exemple.com/ws »).
                   Prioritaire sur host/port, qui ne savent pas exprimer un
                   serveur joint par nom de domaine derrière un proxy HTTPS —
                   voir config.websocket_url. host/port restent le repli des
                   postes configurés par adresse IP.
        """
        self.host = host
        self.port = port
        self.ws_url = ws_url
        self.on_message_callback = on_message_callback
        self.token = token
        self.ticket_provider = ticket_provider
        self.on_giveup_callback = on_giveup_callback
        # Distincts de on_giveup_callback (déclenché une fois, à l'abandon
        # final après ~55 minutes) : ceux-ci marquent le DÉBUT et la FIN de
        # chaque épisode de coupure, pour que l'écran puisse afficher « temps
        # réel interrompu » pendant que le superviseur retente en arrière-plan
        # — sans eux, l'opérateur ne voyait rien pendant potentiellement
        # près d'une heure de tentatives silencieuses, croyant recevoir les
        # mises à jour d'autres postes alors que ce n'était plus le cas.
        # `_episode_deconnexion_signale` évite de rappeler
        # on_disconnect_callback à chaque tentative de reconnexion ratée : un
        # seul signal par épisode, jusqu'au prochain `_on_open` réussi.
        self.on_disconnect_callback = on_disconnect_callback
        self.on_reconnect_callback = on_reconnect_callback
        self._episode_deconnexion_signale = False
        self.ws = None
        self.connected = False
        self.should_reconnect = True
        self.reconnect_interval = 5  # secondes (délai de base)
        self.reconnect_interval_max = 60  # plafond du backoff
        # Budget de reconnexion. 10 tentatives couvraient 5+10+20+40+60*6 ≈ 7
        # minutes : suffisant pour le redémarrage d'un serveur sur le même
        # réseau, très insuffisant pour un poste régional relié par Internet,
        # où une coupure de courant ou un redémarrage de modem dépasse
        # couramment la demi-heure. Le poste abandonnait alors le temps réel
        # pour le reste de la journée. 60 tentatives portent la couverture à
        # ~55 minutes, sans coût : le superviseur dort entre les essais.
        self.max_reconnect_attempts = 60
        self._reconnect_attempts = 0
        # Refus d'authentification (code 1008) tolérés d'affilée. Un seul ne
        # prouve rien sur une liaison lente : le billet ne vit que 30 s, et il
        # est demandé AVANT l'ouverture de la socket. Sur un lien saturé, il
        # peut expirer en vol — le serveur ferme alors en 1008 alors que la
        # session est parfaitement valide. Comme un billet neuf est redemandé à
        # chaque tentative, réessayer a un sens ; seuls des refus répétés
        # signent un jeton réellement invalide.
        self.max_refus_auth = 3
        self._refus_auth = 0
        self.thread = None
        # Un seul thread de reconnexion à la fois (voir _programmer_reconnexion)
        self._reconnect_lock = threading.Lock()
        self._reconnect_thread = None
        # Posé par _programmer_reconnexion, consommé par le superviseur : lui
        # évite de sortir juste au moment où une nouvelle relance arrive.
        self._relance_demandee = False
        self._stop_event = threading.Event()

    def _signaler_deconnexion(self):
        """Prévient l'écran une seule fois par épisode de coupure."""
        if not self._episode_deconnexion_signale:
            self._episode_deconnexion_signale = True
            if self.on_disconnect_callback:
                self.on_disconnect_callback()

    def _delai_reconnexion(self) -> int:
        """Backoff progressif : 5s, 10s, 20s, 40s, puis 60s au maximum."""
        delai = self.reconnect_interval * (2 ** max(0, self._reconnect_attempts - 1))
        return min(int(delai), self.reconnect_interval_max)

    def _programmer_reconnexion(self):
        """Réveille (ou démarre) le superviseur de reconnexion.

        Un seul thread mène TOUTE la séquence de reconnexion, au lieu d'un
        thread par tentative. L'ancienne version se reprogrammait de proche en
        proche, et chaque chemin de sortie qui oubliait de le faire arrêtait
        la reconnexion pour de bon, sans rien dire à l'opérateur :

        * une exception avant `run_forever` (billet illisible, URL invalide) —
          `_on_close` n'est alors jamais appelé, donc rien ne reprogrammait ;
        * un `join()` expiré sur l'ancienne socket — `connect()` refusait de
          démarrer un second thread et retournait sans rien planifier.

        Ici le superviseur reboucle tant qu'il n'est pas connecté : il ne peut
        plus « oublier » de retenter. `_relance_demandee` couvre la course
        entre un superviseur qui sort et un `_on_close` simultané.
        """
        with self._reconnect_lock:
            if not self.should_reconnect:
                return
            self._relance_demandee = True
            if self._reconnect_thread is not None and self._reconnect_thread.is_alive():
                return  # le superviseur tourne déjà, il verra le drapeau
            self._reconnect_thread = threading.Thread(
                target=self._superviser_reconnexion, daemon=True)
            self._reconnect_thread.start()

    def _superviser_reconnexion(self):
        """Retente la connexion jusqu'au succès, à l'abandon ou à disconnect()."""
        while True:
            with self._reconnect_lock:
                if not self._relance_demandee:
                    return
                self._relance_demandee = False

            if not self.should_reconnect or self.connected:
                return
            if self._reconnect_attempts >= self.max_reconnect_attempts:
                logger.warning(
                    "WebSocket : %d tentatives de reconnexion échouées, abandon. "
                    "La connexion sera relancée à la prochaine ouverture de session.",
                    self.max_reconnect_attempts,
                )
                if self.on_giveup_callback:
                    self.on_giveup_callback()
                return

            self._reconnect_attempts += 1
            delai = self._delai_reconnexion()
            logger.info(
                f"Tentative de reconnexion WebSocket #{self._reconnect_attempts} "
                f"dans {delai} secondes..."
            )
            # wait() rend la main immédiatement si disconnect() est appelé.
            if self._stop_event.wait(delai):
                return
            if not self.should_reconnect or self.connected:
                return

            # Laisser l'ancien thread run_forever se terminer avant d'en créer
            # un nouveau, sinon les threads s'accumulent.
            ancien = self.thread
            if ancien is not None and ancien.is_alive():
                ancien.join(timeout=5)
                if ancien.is_alive():
                    # Socket encore bloquée (réseau qui ne répond plus) :
                    # connect() serait un non-événement. On repasse par le
                    # backoff plutôt que d'abandonner.
                    with self._reconnect_lock:
                        self._relance_demandee = True
                    continue

            self.connect()
            # Laisse la tentative s'établir (ou échouer). En cas d'échec,
            # _on_close / _connecter_et_ecouter repositionnent le drapeau et
            # la boucle repart ; en cas de succès, self.connected passe à True
            # et le superviseur sort.
            self._stop_event.wait(2)
            if self.connected:
                return

    def _on_message(self, ws, message):
        """Appelé lorsqu'un message est reçu du serveur."""
        try:
            data = json.loads(message)
            message_type = data.get("type")
            message_data = data.get("data", {})

            logger.debug(f"Message WebSocket reçu : {message_type}")

            if self.on_message_callback:
                self.on_message_callback(message_type, message_data)

        except json.JSONDecodeError as e:
            logger.error(f"Erreur de décodage JSON WebSocket : {e}")
        except Exception as e:
            logger.error(f"Erreur lors du traitement du message WebSocket : {e}")

    def _on_error(self, ws, error):
        """Appelé lorsqu'une erreur survient."""
        logger.error(f"Erreur WebSocket : {error}")
        self.connected = False

    def _on_close(self, ws, close_status_code, close_msg):
        """Appelé lorsque la connexion est fermée."""
        logger.info(f"Connexion WebSocket fermée : {close_status_code} - {close_msg}")
        self.connected = False
        self._signaler_deconnexion()

        # Ne jamais reconnecter depuis ce callback (il tourne dans le thread
        # run_forever) : on délègue à un thread de reconnexion dédié.
        # 1008 « policy violation » : c'est le code que le serveur renvoie
        # quand le token est absent ou invalide (app.py). Le client testait
        # 4001, un code que le serveur n'émet jamais : il reconnectait donc en
        # boucle avec un token déjà refusé.
        if close_status_code == 1008:
            self._refus_auth += 1
            if self._refus_auth >= self.max_refus_auth:
                # Refusé plusieurs fois de suite avec un billet neuf à chaque
                # essai : le jeton de session est en cause, insister est vain.
                logger.warning(
                    "WebSocket refusé (authentification) %d fois : abandon.",
                    self._refus_auth)
                self.should_reconnect = False
                # Sans cet appel, l'abandon était TOTALEMENT muet : ni
                # reconnexion, ni indicateur de repli à l'écran. L'opérateur
                # gardait un tableau de bord figé sans jamais savoir que le
                # temps réel s'était arrêté.
                if self.on_giveup_callback:
                    self.on_giveup_callback()
                return
            logger.warning(
                "WebSocket refusé (authentification), tentative %d/%d : "
                "billet peut-être expiré en vol, on redemande.",
                self._refus_auth, self.max_refus_auth)

        self._programmer_reconnexion()

    def _on_open(self, ws):
        """Appelé lorsque la connexion est établie."""
        logger.info("Connexion WebSocket établie")
        self.connected = True
        self._reconnect_attempts = 0
        self._refus_auth = 0
        if self._episode_deconnexion_signale:
            self._episode_deconnexion_signale = False
            if self.on_reconnect_callback:
                self.on_reconnect_callback()

    def connect(self):
        """Établit la connexion WebSocket."""
        if self.connected:
            return
        # Ne pas empiler les threads : si le précédent tourne encore, on sort.
        if self.thread is not None and self.thread.is_alive():
            return

        # Le billet (HTTP, bloquant) et l'ouverture de la socket se font dans
        # le thread de fond : connect() est appelé depuis le thread Tk lors de
        # la connexion initiale, et ne doit jamais bloquer l'interface.
        self.thread = threading.Thread(target=self._connecter_et_ecouter, daemon=True)
        self.thread.start()

    def _connecter_et_ecouter(self):
        try:
            # Import paresseux : le temps réel est un confort (le polling de
            # secours à 30s prend le relais), pas une dépendance de démarrage.
            # Un import au niveau module empêchait TOUTE l'application de
            # démarrer si le paquet websocket-client manquait sur le poste.
            try:
                import websocket  # websocket-client
            except ImportError:
                logger.warning(
                    "Module 'websocket-client' absent : temps réel désactivé, "
                    "le polling de secours prend le relais."
                )
                self.should_reconnect = False
                if self.on_giveup_callback:
                    self.on_giveup_callback()
                return

            # Construire l'URL WebSocket avec le billet d'authentification en
            # paramètre de requête. Le billet (30s, usage unique) est préféré
            # au token complet (8h) : il limite ce qui reste exposé dans les
            # logs d'accès et tout proxy intermédiaire s'il y fuite. Redemandé
            # à chaque connexion : un billet déjà consommé ne fonctionne plus.
            ticket = self.ticket_provider() if self.ticket_provider else None
            ws_url = self.ws_url or f"ws://{self.host}:{self.port}/ws"
            if ticket:
                ws_url += f"?ticket={ticket}"
            elif self.token:
                logger.warning(
                    "WebSocket : pas de billet disponible, repli sur le token complet."
                )
                ws_url += f"?token={self.token}"

            # Jamais la chaîne de requête dans les logs : c'est justement ce
            # que le billet à usage unique cherche à limiter (commentaire
            # ci-dessus) — mais le repli sur le token complet (8h) ne doit
            # pas défaire cette protection en le loggant quand même.
            logger.info(f"Connexion à {ws_url.split('?', 1)[0]}")

            self.ws = websocket.WebSocketApp(
                ws_url,
                on_open=self._on_open,
                on_message=self._on_message,
                on_error=self._on_error,
                on_close=self._on_close
            )

            # ping_interval/ping_timeout : sans heartbeat, une coupure réseau
            # silencieuse (câble débranché, veille du serveur) laisse la socket
            # ouverte indéfiniment côté poste — l'écran semble à jour alors
            # qu'il ne reçoit plus rien. Le ping force la détection en ~40 s.
            self.ws.run_forever(ping_interval=30, ping_timeout=10)

        except Exception as e:
            # Tout ce qui casse AVANT que run_forever ne prenne la main :
            # billet illisible (une réponse HTML de proxy au lieu du JSON
            # attendu fait lever JSONDecodeError, que get_ws_ticket ne
            # rattrape pas — ce n'est pas une ApiError), URL invalide,
            # WebSocketApp qui refuse de se construire. Dans ce cas _on_close
            # n'est jamais appelé : sans la relance ci-dessous, aucune
            # reconnexion n'était planifiée et on_giveup_callback n'était pas
            # appelé non plus. Le poste perdait le temps réel définitivement,
            # sans même l'indicateur qui prévient l'opérateur.
            logger.error(f"Erreur lors de la connexion WebSocket : {e}")
            self.connected = False
            self._signaler_deconnexion()
            self._programmer_reconnexion()

    def disconnect(self):
        """Ferme la connexion WebSocket."""
        self.should_reconnect = False
        # Réveille immédiatement un éventuel thread de reconnexion en attente.
        self._stop_event.set()
        if self.ws:
            try:
                self.ws.close()
            except Exception as e:
                logger.error(f"Erreur lors de la fermeture du WebSocket : {e}")
        self.connected = False

    def send_message(self, message):
        """Envoie un message au serveur (optionnel pour les futures extensions)."""
        if self.connected and self.ws:
            try:
                self.ws.send(json.dumps(message))
            except Exception as e:
                logger.error(f"Erreur lors de l'envoi du message WebSocket : {e}")