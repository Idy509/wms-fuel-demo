"""Point d'entrée du serveur WMS, pour lancement direct ou empaquetage PyInstaller.

En développement : python lancer_serveur.py
Empaqueté       : wms_serveur.exe

Adresse d'écoute réglable par variables d'environnement, sans recompiler :

    WMS_HOST  (défaut 0.0.0.0)  interface d'écoute
    WMS_PORT  (défaut 8000)     port TCP

`0.0.0.0` écoute sur TOUTES les interfaces de la machine : c'est ce qu'il faut
quand les postes clients sont d'autres ordinateurs du réseau local (le cas
d'aujourd'hui), et c'est pour cela que ça reste le défaut. Si le serveur et le
client tournent sur LA MÊME machine, poser `WMS_HOST=127.0.0.1` suffit : le
port n'est alors plus joignable depuis le réseau du tout, et aucune règle de
pare-feu n'est nécessaire.
"""
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import uvicorn

# Import explicite (et non la chaîne "app:app") : PyInstaller analyse les
# imports statiquement. Avec une chaîne, le module `app` et tout ce qu'il
# importe n'étaient jamais empaquetés et l'exe ne démarrait pas.
from app import app as application
from database import BaseSurLecteurReseau, _verifier_base_locale

HOTE_DEFAUT = "0.0.0.0"
PORT_DEFAUT = 8000


def hote_ecoute() -> str:
    """Interface d'écoute : `WMS_HOST`, ou toutes les interfaces par défaut."""
    return (os.environ.get("WMS_HOST") or "").strip() or HOTE_DEFAUT


def port_ecoute() -> int:
    """Port d'écoute : `WMS_PORT`, ou 8000. Une valeur illisible ne bloque pas
    le démarrage — elle est signalée et le défaut reprend la main, plutôt que
    de laisser le serveur refuser de se lancer sur une faute de frappe."""
    brut = (os.environ.get("WMS_PORT") or "").strip()
    if not brut:
        return PORT_DEFAUT
    try:
        port = int(brut)
    except ValueError:
        port = 0
    if not (1 <= port <= 65535):
        print(f"WMS_PORT invalide ({brut!r}) — port {PORT_DEFAUT} utilisé.")
        return PORT_DEFAUT
    return port


def main():
    # Contrôle AVANT uvicorn : `init_db()` refait la même vérification depuis
    # le lifespan, mais l'échec y sort sous forme de trace Python au milieu
    # des logs uvicorn. Ici, l'opérateur lit une phrase et rien d'autre.
    try:
        _verifier_base_locale()
    except BaseSurLecteurReseau as e:
        print("\n" + "=" * 70)
        print("DÉMARRAGE REFUSÉ")
        print("=" * 70)
        print(e)
        print("=" * 70 + "\n")
        sys.exit(1)
    uvicorn.run(application, host=hote_ecoute(), port=port_ecoute(),
                log_level="info")


if __name__ == "__main__":
    main()
