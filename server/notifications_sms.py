"""Point de branchement SMS / WhatsApp — DÉLIBÉRÉMENT VIDE.

Ce module n'envoie rien. Il existe pour que le code appelant (alertes de
rupture, astreinte) ait dès aujourd'hui un point d'entrée stable, sans que le
projet dépende d'un fournisseur qui n'a pas encore été choisi, ni d'un compte
payant qu'il faudrait créer pour simplement lancer les tests.

`envoyer_sms` journalise et renvoie False. C'est le comportement définitif
tant qu'aucun fournisseur n'est branché — pas un bug, pas un TODO oublié.

Le jour où un fournisseur est retenu (Twilio, Vonage, une passerelle locale,
l'API WhatsApp Business), l'implémentation se limite à remplacer le corps de
`envoyer_sms`, sans toucher aux appelants. À titre d'exemple, avec Twilio,
elle ressemblerait à ceci (COMMENTAIRE, pas du code actif — `twilio` n'est
pas et ne doit pas devenir une dépendance tant que rien n'est décidé) :

    # from twilio.rest import Client
    #
    # config = _charger_config()          # comme notifications_email
    # if config is None:
    #     return False
    # try:
    #     client = Client(config["account_sid"], config["auth_token"])
    #     client.messages.create(to=destinataire,
    #                            from_=config["numero_expediteur"],
    #                            body=message)
    # except Exception:
    #     log.exception("échec de l'envoi de SMS")
    #     return False
    # return True

La configuration suivrait le même patron que l'e-mail : un fichier JSON édité
à la main sur le serveur, absent par défaut, absence = fonction désactivée.
"""
from journalisation import logger

log = logger(__name__)


def sms_actif() -> bool:
    """Toujours False : aucun fournisseur n'est branché. Voir l'en-tête."""
    return False


def envoyer_sms(destinataire: str, message: str) -> bool:
    """Point d'extension : journalise et renvoie False, sans jamais lever.

    Signature figée pour que le branchement futur d'un fournisseur ne demande
    aucune modification des appelants.
    """
    log.warning("SMS non envoyé : aucun fournisseur configuré (destinataire=%s)",
                destinataire)
    return False
