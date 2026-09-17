"""RESTAURATION D'UNE SAUVEGARDE DE LA BASE — outil de dépannage.

À QUOI ÇA SERT
--------------
Le serveur fabrique tout seul une sauvegarde de la base toutes les 6 heures
(voir INTERVAL_SECONDS dans backup.py). Ce script sert à REMETTRE une de ces sauvegardes à la place de la
base actuelle, en cas de gros problème : base corrompue après une coupure de
courant, données effacées par erreur, disque remplacé.

Ce n'est PAS un outil du quotidien. On ne s'en sert que quand quelque chose
s'est vraiment mal passé.

COMMENT S'EN SERVIR
-------------------
1. ARRÊTER LE SERVEUR d'abord (fermer la fenêtre du serveur).
   Restaurer pendant que le serveur tourne donnerait une base incohérente.
2. Lancer ce fichier :
      - double-clic sur restaurer_sauvegarde.py, ou
      - dans une invite de commande :  python restaurer_sauvegarde.py
3. Le script affiche la liste des sauvegardes, de la plus récente à la plus
   ancienne. Taper le numéro de celle à restaurer, puis Entrée.
4. Confirmer en tapant OUI.
5. Relancer le serveur.

FORMAT DES SAUVEGARDES
----------------------
Une sauvegarde est une simple archive gzip de la base :
`warehouse_AAAAMMJJ_HHMMSS.db.gz`. Elle n'est pas chiffrée : on peut aussi la
décompresser à la main avec 7-Zip, le fichier obtenu est directement une base
SQLite.

Les archives `.enc.db.gz` produites par les anciennes versions (chiffrées)
restent lisibles par ce script tant que le module `cryptography` est présent.

SÉCURITÉ
--------
Avant de remplacer quoi que ce soit, le script met la base actuelle de côté
sous le nom `warehouse_avant_restauration_AAAAMMJJ_HHMMSS.db`. Rien n'est
jamais effacé définitivement : en cas de mauvaise manipulation, ce fichier
permet de revenir en arrière.

ATTENTION : les mouvements enregistrés APRÈS la date de la sauvegarde choisie
ne seront plus dans la base restaurée. Choisir la sauvegarde la plus récente
qui précède le problème.
"""
import gzip
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from chemins import chemin_base, dossier_sauvegardes  # noqa: E402

# En-tête d'un fichier SQLite. Sert à distinguer une archive au format actuel
# (non chiffrée) d'une archive chiffrée produite par une ancienne version.
ENTETE_SQLITE = b"SQLite format 3\x00"


def _taille_lisible(octets: int) -> str:
    if octets < 1024:
        return f"{octets} o"
    if octets < 1024 * 1024:
        return f"{octets / 1024:.0f} Ko"
    return f"{octets / (1024 * 1024):.1f} Mo"


def _date_sauvegarde(fichier: Path) -> datetime | None:
    """Date lue dans le nom du fichier (warehouse_AAAAMMJJ_HHMMSS.db.gz)."""
    nom = fichier.name.replace("warehouse_", "")
    try:
        return datetime.strptime(nom[:15], "%Y%m%d_%H%M%S")
    except ValueError:
        return None


def lister_sauvegardes(dossier: Path) -> list[Path]:
    """Sauvegardes disponibles, de la plus récente à la plus ancienne.

    Le motif large `warehouse_*.gz` couvre le format actuel (.db.gz) comme les
    archives chiffrées des anciennes versions (.enc.db.gz), qui doivent rester
    restaurables après une mise à jour.
    """
    if not dossier.exists():
        return []
    fichiers = list(dossier.glob("warehouse_*.gz"))
    fichiers.sort(key=lambda f: _date_sauvegarde(f) or datetime.min, reverse=True)
    return fichiers


def _dechiffrer_archive_ancienne(donnees: bytes) -> bytes:
    """Déchiffre une archive .enc.db.gz produite par une ancienne version.

    Le chiffrement a été retiré (voir backup.py), mais les archives déjà sur
    disque doivent rester restaurables. La clé était dérivée de constantes
    livrées avec l'application : elle est reproductible telle quelle.
    """
    import base64
    import os

    from cryptography.fernet import Fernet
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

    kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                     salt=b"wms_backup_salt_fixed_", iterations=100000)
    mot_de_passe = os.environ.get(
        "WMS_BACKUP_PASSWORD",
        "default_backup_password_change_in_production").encode()
    cle = base64.urlsafe_b64encode(kdf.derive(mot_de_passe))
    return Fernet(cle).decrypt(donnees)


def verifier_archive(archive: Path, destination_test: Path) -> bool:
    """Décompresse dans un fichier temporaire, vérifie que SQLite l'accepte.

    Une archive tronquée par une coupure pendant l'écriture ne doit surtout pas
    remplacer une base encore lisible.
    """
    with gzip.open(archive, "rb") as f_in:
        contenu = f_in.read()
    if not contenu.startswith(ENTETE_SQLITE):
        # Archive chiffrée d'une ancienne version.
        contenu = _dechiffrer_archive_ancienne(contenu)
    destination_test.write_bytes(contenu)
    conn = sqlite3.connect(destination_test)
    try:
        resultat = conn.execute("PRAGMA integrity_check").fetchone()
    finally:
        conn.close()
    return bool(resultat) and resultat[0] == "ok"


def main() -> int:
    base = chemin_base()
    dossier = dossier_sauvegardes()

    print()
    print("=" * 66)
    print("  RESTAURATION D'UNE SAUVEGARDE DE LA BASE")
    print("=" * 66)
    print()
    print(f"Base actuelle      : {base}")
    print(f"Dossier sauvegardes: {dossier}")
    print()
    print("IMPORTANT : le serveur doit être ARRÊTÉ avant de continuer.")
    print()

    sauvegardes = lister_sauvegardes(dossier)
    if not sauvegardes:
        print("Aucune sauvegarde trouvée dans ce dossier.")
        print("Rien à restaurer. Le script s'arrête sans rien modifier.")
        input("\nAppuie sur Entrée pour fermer...")
        return 1

    print(f"{len(sauvegardes)} sauvegarde(s) disponible(s) "
          f"(la n°1 est la plus récente) :")
    print()
    for i, fichier in enumerate(sauvegardes, 1):
        date = _date_sauvegarde(fichier)
        libelle = date.strftime("%d/%m/%Y à %H:%M:%S") if date else "date inconnue"
        print(f"  {i:>3}. {libelle:<26} {_taille_lisible(fichier.stat().st_size):>9}")
    print()

    reponse = input("Numéro de la sauvegarde à restaurer (ou Entrée pour annuler) : ").strip()
    if not reponse:
        print("Annulé. Rien n'a été modifié.")
        input("\nAppuie sur Entrée pour fermer...")
        return 0
    try:
        index = int(reponse)
        if not 1 <= index <= len(sauvegardes):
            raise ValueError
    except ValueError:
        print(f"'{reponse}' n'est pas un numéro valide de la liste. Rien n'a été modifié.")
        input("\nAppuie sur Entrée pour fermer...")
        return 1

    choisie = sauvegardes[index - 1]
    date = _date_sauvegarde(choisie)
    libelle = date.strftime("%d/%m/%Y à %H:%M:%S") if date else choisie.name

    print()
    print("-" * 66)
    print(f"Sauvegarde choisie : {libelle}")
    print()
    print("La base actuelle sera remplacée par cette sauvegarde.")
    print("Tous les mouvements enregistrés APRÈS cette date seront perdus")
    print("(une copie de la base actuelle sera tout de même conservée).")
    print("-" * 66)
    print()
    if input("Taper OUI en majuscules pour confirmer : ").strip() != "OUI":
        print("Annulé. Rien n'a été modifié.")
        input("\nAppuie sur Entrée pour fermer...")
        return 0

    horodatage = datetime.now().strftime("%Y%m%d_%H%M%S")
    provisoire = base.parent / f"warehouse_restauration_{horodatage}.tmp"
    base.parent.mkdir(parents=True, exist_ok=True)

    print()
    print("[1/4] Décompression et vérification de la sauvegarde...")
    try:
        if not verifier_archive(choisie, provisoire):
            print("      ÉCHEC : cette sauvegarde est abîmée et ne peut pas être")
            print("      utilisée. Rien n'a été modifié. Essaie la sauvegarde")
            print("      précédente dans la liste.")
            provisoire.unlink(missing_ok=True)
            input("\nAppuie sur Entrée pour fermer...")
            return 1
    except Exception as e:
        print(f"      ÉCHEC : impossible de lire la sauvegarde ({e}).")
        print("      Rien n'a été modifié.")
        provisoire.unlink(missing_ok=True)
        input("\nAppuie sur Entrée pour fermer...")
        return 1
    print("      Sauvegarde lisible et cohérente.")

    print("[2/4] Mise de côté de la base actuelle...")
    copie_securite = None
    if base.exists():
        copie_securite = base.parent / f"warehouse_avant_restauration_{horodatage}.db"
        try:
            shutil.copy2(base, copie_securite)
            # La base tourne en WAL : les dernières transactions validées
            # peuvent n'exister que dans le fichier -wal, pas encore dans
            # le .db. Copier le .db seul puis SUPPRIMER le -wal à l'étape
            # suivante détruisait définitivement cette queue d'écritures —
            # alors que le script promet que rien n'est jamais perdu.
            for suffixe in ("-wal", "-shm"):
                annexe = Path(str(base) + suffixe)
                if annexe.exists():
                    shutil.copy2(annexe, Path(str(copie_securite) + suffixe))
        except OSError as e:
            print(f"      ÉCHEC : impossible de copier la base actuelle ({e}).")
            print("      Le serveur tourne peut-être encore. Arrête-le et")
            print("      relance ce script. Rien n'a été modifié.")
            provisoire.unlink(missing_ok=True)
            input("\nAppuie sur Entrée pour fermer...")
            return 1
        print(f"      Copie conservée : {copie_securite.name}")
    else:
        print("      Aucune base actuelle : rien à mettre de côté.")

    print("[3/4] Remplacement de la base...")
    try:
        provisoire.replace(base)
        # Les fichiers -wal et -shm appartiennent à l'ANCIENNE base : les
        # laisser ferait relire des transactions qui n'ont plus de sens.
        for suffixe in ("-wal", "-shm"):
            Path(str(base) + suffixe).unlink(missing_ok=True)
    except OSError as e:
        print(f"      ÉCHEC : {e}")
        print("      Le serveur tourne peut-être encore.")
        if copie_securite:
            print(f"      La base d'origine reste disponible : {copie_securite.name}")
        provisoire.unlink(missing_ok=True)
        input("\nAppuie sur Entrée pour fermer...")
        return 1
    print("      Base remplacée.")

    print("[4/4] Vérification finale...")
    conn = sqlite3.connect(base)
    try:
        nb_docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        nb_produits = conn.execute("SELECT COUNT(*) FROM products").fetchone()[0]
        print(f"      {nb_produits} produit(s), {nb_docs} bon(s) dans la base restaurée.")
    except sqlite3.Error as e:
        print(f"      Avertissement : lecture partielle ({e}).")
    finally:
        conn.close()

    print()
    print("=" * 66)
    print("  RESTAURATION TERMINÉE")
    print("=" * 66)
    print(f"  Sauvegarde restaurée : {libelle}")
    if copie_securite:
        print(f"  Ancienne base gardée : {copie_securite}")
    print()
    print("  Tu peux relancer le serveur.")
    print()
    input("Appuie sur Entrée pour fermer...")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\nInterrompu. Rien n'a été modifié.")
        sys.exit(1)
