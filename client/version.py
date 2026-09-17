"""Version du client — une seule ligne à changer à chaque nouvelle publication.

Comparée à `GET /version` sur le serveur (fichier édité à la main par l'admin)
pour afficher un bandeau « nouvelle version disponible ». Rien d'automatique :
c'est l'admin qui décide quand annoncer une version, et l'opérateur qui décide
quand réinstaller.
"""

VERSION = "1.0.0"
