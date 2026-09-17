"""Envoi d'e-mails (alertes de rupture, rapport mensuel).

Configuration par fichier JSON édité à la main sur le serveur, exactement
comme `version.json` : il n'y a pas d'écran pour le saisir, parce qu'un
réglage qu'on touche une fois à l'installation n'a pas à vivre dans
l'application. Emplacement : `chemins.chemin_email_config()` (par défaut
`email_config.json` à la racine des données, à côté de `version.json`).

Contenu attendu :

    {
      "smtp_host": "smtp.exemple.com",
      "smtp_port": 587,
      "smtp_user": "wms@exemple.com",
      "smtp_password": "...",
      "expediteur": "wms@exemple.com",
      "destinataires_alertes": ["chef@exemple.com"],
      "mode": "immediat"
    }

Clé `mode` (facultative) : rythme d'envoi des ALERTES (ruptures de seuil,
signalements régionaux, réconciliation). Deux valeurs :

  - "immediat"        : un e-mail par événement, dès qu'il survient. C'est le
                        comportement HISTORIQUE, et celui qui s'applique quand
                        la clé est absente, vide, ou porte une valeur
                        inconnue — une faute de frappe ne doit pas éteindre
                        les alertes en silence.
  - "digest_quotidien" : rien n'est envoyé sur le moment ; les événements sont
                        accumulés en mémoire et partent en UN SEUL e-mail par
                        jour. Pour une boîte qui reçoit trop d'alertes.

Le rapport mensuel (`/reports/mensuel/envoyer-maintenant`) n'est pas concerné :
il est déclenché à la main par un administrateur, qui attend son envoi.

RÈGLE ABSOLUE : `envoyer_email` ne lève JAMAIS. Fichier absent, incomplet,
illisible, serveur SMTP injoignable, mot de passe refusé — tout se solde par
un `False` et une ligne de journal. Cette fonction est appelée depuis des
chemins qui doivent réussir sans elle (création d'un bon, sauvegarde) : une
exception qui remonte y ferait échouer une écriture métier légitime pour un
mail qui n'est pas parti.
"""
import json
import smtplib
import threading
from email.message import EmailMessage

from chemins import chemin_email_config
from journalisation import logger

log = logger(__name__)

# Un serveur SMTP qui ne répond pas ne doit pas immobiliser un thread de fond
# indéfiniment. 20 s : large pour un relais lent, court devant l'intervalle
# entre deux sauvegardes ou deux alertes.
DELAI_SMTP = 20

# Champs sans lesquels l'envoi est impossible. `smtp_user` / `smtp_password`
# n'en font pas partie : un relais interne d'entreprise accepte souvent un
# envoi sans authentification.
CHAMPS_REQUIS = ("smtp_host", "expediteur")


def charger_config() -> dict | None:
    """Réglages SMTP, ou None si absents/illisibles/incomplets.

    Aucune exception ne sort d'ici : l'absence de configuration est l'état
    normal d'une installation qui n'utilise pas les e-mails.
    """
    chemin = chemin_email_config()
    try:
        data = json.loads(chemin.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        log.warning("configuration e-mail illisible (%s) : envoi désactivé", chemin)
        return None
    if not isinstance(data, dict):
        log.warning("configuration e-mail invalide (%s) : envoi désactivé", chemin)
        return None
    manquants = [c for c in CHAMPS_REQUIS if not str(data.get(c) or "").strip()]
    if manquants:
        log.warning("configuration e-mail incomplète (%s manquant) : envoi désactivé",
                    ", ".join(manquants))
        return None
    return data


def destinataires_alertes() -> list[str]:
    """Adresses à prévenir en cas de rupture critique. Vide si non configuré."""
    config = charger_config()
    if config is None:
        return []
    valeur = config.get("destinataires_alertes") or []
    if isinstance(valeur, str):
        valeur = [valeur]
    if not isinstance(valeur, list):
        return []
    return [str(a).strip() for a in valeur if str(a).strip()]


def email_actif() -> bool:
    """Vrai si une configuration SMTP exploitable est présente."""
    return charger_config() is not None


# Rythme d'envoi des alertes. `MODE_IMMEDIAT` est le comportement historique
# et le SEUL par défaut : toute autre valeur que `MODE_DIGEST` y retombe.
MODE_IMMEDIAT = "immediat"
MODE_DIGEST = "digest_quotidien"
MODES_VALIDES = (MODE_IMMEDIAT, MODE_DIGEST)


def mode_alertes() -> str:
    """Rythme d'envoi configuré : "immediat" (défaut) ou "digest_quotidien".

    Ne lève jamais et ne renvoie jamais autre chose qu'un mode connu. Une clé
    absente, vide, mal orthographiée ou d'un type inattendu donne
    `MODE_IMMEDIAT` : le mode qui envoie, plutôt que celui qui retient. Une
    valeur inconnue est signalée au journal — l'administrateur qui a tapé
    "digest" au lieu de "digest_quotidien" doit pouvoir le découvrir.
    """
    config = charger_config()
    if config is None:
        return MODE_IMMEDIAT
    valeur = str(config.get("mode") or "").strip().lower()
    if not valeur:
        return MODE_IMMEDIAT
    if valeur in MODES_VALIDES:
        return valeur
    log.warning("mode d'envoi e-mail inconnu (%r) : mode immédiat conservé. "
                "Valeurs acceptées : %s", valeur, ", ".join(MODES_VALIDES))
    return MODE_IMMEDIAT


def digest_actif() -> bool:
    """Vrai si les alertes doivent être groupées dans un envoi quotidien."""
    return mode_alertes() == MODE_DIGEST


def envoyer_email(destinataires: list[str], sujet: str, corps: str) -> bool:
    """Envoie un e-mail texte. Renvoie True seulement si le relais l'a accepté.

    Ne lève jamais : voir l'en-tête du module.
    """
    adresses = [str(a).strip() for a in (destinataires or []) if str(a).strip()]
    if not adresses:
        log.warning("e-mail non envoyé : aucun destinataire")
        return False

    config = charger_config()
    if config is None:
        log.warning("e-mail non envoyé : aucune configuration SMTP (%s)",
                    chemin_email_config())
        return False

    message = EmailMessage()
    message["From"] = config["expediteur"]
    message["To"] = ", ".join(adresses)
    message["Subject"] = sujet
    message.set_content(corps)

    hote = str(config["smtp_host"]).strip()
    try:
        port = int(config.get("smtp_port") or 587)
    except (TypeError, ValueError):
        port = 587
    utilisateur = str(config.get("smtp_user") or "").strip()
    motdepasse = str(config.get("smtp_password") or "")

    try:
        # Port 465 = SMTPS (TLS dès la connexion) ; les autres ports passent
        # par STARTTLS, refusé sans bruit par les relais qui ne le gèrent pas
        # — d'où le try autour de starttls() plutôt qu'un échec sec.
        if port == 465:
            serveur = smtplib.SMTP_SSL(hote, port, timeout=DELAI_SMTP)
        else:
            serveur = smtplib.SMTP(hote, port, timeout=DELAI_SMTP)
        with serveur:
            if port != 465:
                try:
                    serveur.starttls()
                except smtplib.SMTPException:
                    log.info("relais SMTP %s sans STARTTLS : envoi en clair", hote)
            if utilisateur:
                serveur.login(utilisateur, motdepasse)
            serveur.send_message(message)
    except Exception:
        # Volontairement large : socket, DNS, SSL, SMTP, encodage. Aucun de
        # ces échecs ne doit remonter à l'appelant.
        log.exception("échec de l'envoi d'e-mail vers %s", ", ".join(adresses))
        return False

    log.info("e-mail envoyé à %s (sujet : %s)", ", ".join(adresses), sujet)
    return True


def envoyer_en_arriere_plan(destinataires: list[str], sujet: str, corps: str) -> None:
    """Lance l'envoi dans un thread détaché et rend la main immédiatement.

    Appelée depuis la création d'un bon : la réponse HTTP de l'opérateur ne
    doit pas attendre un relais SMTP, qui peut mettre plusieurs secondes.
    Thread daemon : si le serveur s'arrête pendant l'envoi, il ne bloque pas
    l'arrêt — une alerte non partie vaut mieux qu'un serveur qui ne s'éteint
    pas. Rien n'est levé ici, `envoyer_email` avale déjà tout.

    Court-circuit si rien n'est configuré : sans lui, chaque bon créé sur une
    installation sans e-mail démarrerait un thread pour ne rien faire.
    """
    if not destinataires or not email_actif():
        return
    threading.Thread(
        target=envoyer_email, args=(destinataires, sujet, corps),
        daemon=True, name="envoi-email",
    ).start()
