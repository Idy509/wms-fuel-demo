"""Traduction de l'interface client (français / anglais / créole haïtien).

Le français est la langue de référence : les clés du dictionnaire sont les
textes français eux-mêmes, tels qu'ils apparaissent dans le code. Une clé
absente n'est pas une erreur — `t()` renvoie alors le français tel quel. Un
libellé français affiché en mode anglais est un défaut cosmétique, jamais un
plantage.

Le créole (`"ht"`) n'est PAS traduit intégralement, délibérément : seuls les
écrans qu'un compte régional utilise réellement (Réception régionale,
Inventaire régional, Alertes régionales, connexion) portent une clé `"ht"`.
Partout ailleurs, l'absence de clé fait retomber `t()` sur le français, ce qui
est le comportement voulu — un écran d'administration central en français
plutôt qu'une traduction approximative de 800 libellés.

La langue est un réglage de poste (voir config.py, clé "language"). Elle est
fixée une fois au démarrage par main.py, avant la construction de l'interface :
les vues sont construites puis mises en cache, un changement à chaud ne
retraduirait pas les écrans déjà bâtis. L'écran Paramètres invite donc à
redémarrer.

NE PAS traduire : noms propres (régions, produits, utilisateurs), données
métier venant de la base, identifiants internes (clés de dictionnaire, noms de
colonnes techniques).
"""

_langue = "fr"

LANGUES = ("fr", "en", "ht")

# Libellés des langues dans leur propre langue : un locuteur créolophone
# reconnaît « Kreyòl », pas « Créole haïtien ». Utilisé par les sélecteurs de
# langue de main.py — une seule liste, pour ne pas les voir diverger.
LIBELLES_LANGUES: dict[str, str] = {
    "fr": "Français",
    "en": "English",
    "ht": "Kreyòl",
}


def definir_langue(code: str) -> None:
    """Fixe la langue courante. Toute valeur inconnue retombe sur le français."""
    global _langue
    # `isinstance` avant l'appartenance : la langue vient de la configuration
    # du poste, un fichier éditable à la main. Une valeur non hachable
    # (`"language": []`) ferait lever `in` et le poste ne démarrerait plus.
    _langue = code if isinstance(code, str) and code in LANGUES else "fr"


def langue_courante() -> str:
    return _langue


def t(texte_fr: str) -> str:
    """Renvoie la traduction du texte, ou le français d'origine à défaut.

    Ne lève JAMAIS : une entrée malformée (valeur écrite en chaîne au lieu du
    dictionnaire `{"en": ...}`, traduction non textuelle) retombe sur le
    français. Un dictionnaire de 750 entrées édité à la main finira par en
    contenir une ; elle ne doit pas faire tomber l'écran qui l'affiche.

    Le repli vaut pour TOUTE langue, pas seulement l'anglais : c'est ce qui
    permet au créole de ne couvrir que les écrans régionaux (`entree.get("ht")`
    renvoie None ailleurs, et le français ressort).
    """
    if _langue == "fr":
        return texte_fr
    entree = TRADUCTIONS.get(texte_fr)
    if not isinstance(entree, dict):
        return texte_fr
    traduction = entree.get(_langue)
    return traduction if isinstance(traduction, str) and traduction else texte_fr


TRADUCTIONS: dict[str, dict[str, str]] = {
    # ---------------------------------------------------------------- général
    "Enregistrer": {"en": "Save"},
    "Annuler": {"en": "Cancel", "ht": "Anile"},
    "Fermer": {"en": "Close", "ht": "Fèmen"},
    "Supprimer": {"en": "Delete"},
    "Modifier": {"en": "Edit"},
    "Ajouter": {"en": "Add"},
    "Rechercher": {"en": "Search", "ht": "Chèche"},
    "Recherche": {"en": "Search"},
    "Actualiser": {"en": "Refresh"},
    "Confirmer": {"en": "Confirm"},
    "Confirmer :": {"en": "Confirm:"},
    "Valider": {"en": "Submit"},
    "Retour": {"en": "Return"},
    "Erreur": {"en": "Error", "ht": "Erè"},
    "Succès": {"en": "Success"},
    "Attention": {"en": "Warning"},
    "Information": {"en": "Information"},
    "OK": {"en": "OK"},
    "Oui": {"en": "Yes"},
    "Non": {"en": "No"},
    "Aucun": {"en": "None"},
    "Aucune donnée": {"en": "No data"},
    "Chargement...": {"en": "Loading...", "ht": "Ap chaje..."},
    "Chargement…": {"en": "Loading…", "ht": "Ap chaje…"},
    "Tous": {"en": "All"},
    "Toutes": {"en": "All"},
    "Total": {"en": "Total"},
    "Date": {"en": "Date", "ht": "Dat"},
    "Type": {"en": "Type"},
    "Produit": {"en": "Product"},
    "Produits": {"en": "Products"},
    "Quantité": {"en": "Quantity"},
    "Qté": {"en": "Qty"},
    "Stock": {"en": "Stock"},
    "Unité": {"en": "Unit", "ht": "Inite"},
    "Référence": {"en": "Reference", "ht": "Referans"},
    "Description": {"en": "Description"},
    "Nom": {"en": "Name"},
    "Rôle": {"en": "Role"},
    "Région": {"en": "Region"},
    "Statut": {"en": "Status"},
    "Opérateur": {"en": "Operator"},
    "Commentaire": {"en": "Comment", "ht": "Kòmantè"},
    "Note": {"en": "Note"},
    "Exporter": {"en": "Export"},
    "Importer": {"en": "Import"},
    "Imprimer": {"en": "Print"},
    "Détails": {"en": "Details"},
    "Catégorie": {"en": "Category"},
    "Emplacement": {"en": "Location"},
    "Seuil": {"en": "Threshold"},
    "Client": {"en": "Customer"},
    "Fournisseur": {"en": "Supplier"},
    "Tiers": {"en": "Party"},

    # ------------------------------------------------------- connexion / main
    "Connexion": {"en": "Sign in", "ht": "Koneksyon"},
    "Identifiant :": {"en": "Username:", "ht": "Non itilizatè :"},
    "Mot de passe :": {"en": "Password:", "ht": "Modpas :"},
    "Se connecter": {"en": "Sign in", "ht": "Konekte"},
    "Connexion...": {"en": "Signing in...", "ht": "Ap konekte..."},
    "Connexion au serveur...": {"en": "Connecting to server...", "ht": "Ap konekte ak sèvè a..."},
    "Remplis les deux champs": {"en": "Fill in both fields", "ht": "Ranpli tou de kaz yo"},
    "Créer le compte administrateur": {"en": "Create administrator account"},
    "Premier lancement": {"en": "First launch"},
    "Crée le compte administrateur.": {"en": "Create the administrator account."},
    "Nom complet :": {"en": "Full name:"},
    "Créer et se connecter": {"en": "Create and sign in"},
    "Création en cours...": {"en": "Creating..."},
    "Tous les champs sont requis (mot de passe 12+ car.)":
        {"en": "All fields are required (password 12+ chars)"},
    "Connexion au serveur": {"en": "Server connection"},
    "Configuration serveur": {"en": "Server configuration"},
    "Adresse IP du serveur :": {"en": "Server IP address:"},
    "Port :": {"en": "Port:"},
    "Ton nom (opérateur) :": {"en": "Your name (operator):"},
    "Proposer d'imprimer après chaque bon":
        {"en": "Offer to print after each document"},
    "Langue de l'interface :": {"en": "Interface language:"},
    "Français": {"en": "French"},
    "Anglais": {"en": "English"},
    "Langue modifiée": {"en": "Language changed"},
    "La langue sera appliquée au prochain démarrage de l'application.\n\n"
    "Ferme et relance le programme pour voir l'interface en anglais.":
        {"en": "The language will be applied the next time the application "
               "starts.\n\nClose and relaunch the program to see the "
               "interface in the new language."},
    "Changer mon mot de passe": {"en": "Change my password"},
    "Mot de passe actuel :": {"en": "Current password:"},
    "Nouveau mot de passe :": {"en": "New password:"},
    "Changer": {"en": "Change"},
    "10 caractères minimum": {"en": "10 characters minimum"},
    "Mot de passe trop courant, choisissez-en un autre":
        {"en": "Password too common, choose another one"},
    "Le mot de passe ne doit pas contenir l'identifiant":
        {"en": "The password must not contain the username"},
    "Les mots de passe ne correspondent pas": {"en": "Passwords do not match"},
    "Envoi en cours…": {"en": "Sending…"},
    "Mot de passe changé avec succès": {"en": "Password changed successfully"},
    "Gestion d'entrepôt": {"en": "Warehouse Management"},
    "WMS Entrepôt": {"en": "WMS Warehouse"},
    "Poste local": {"en": "Local station"},

    # --------------------------------------------------------- navigation
    "OPÉRATIONS": {"en": "OPERATIONS"},
    "MOUVEMENTS": {"en": "MOVEMENTS"},
    "DONNÉES": {"en": "DATA"},
    "ADMINISTRATION": {"en": "ADMINISTRATION"},
    "MA RÉGION": {"en": "MY REGION"},
    "Tableau de bord": {"en": "Dashboard"},
    "Réception": {"en": "Receiving"},
    "Expédition": {"en": "Shipping"},
    "Inventaire": {"en": "Inventory"},
    "Bouteilles": {"en": "Bottles"},
    "Historique": {"en": "History"},
    "Contacts": {"en": "Contacts"},
    "Seuils": {"en": "Thresholds"},
    "Entrepôts régionaux": {"en": "Regional warehouses"},
    "Utilisateurs": {"en": "Users"},
    "Journal d'audit": {"en": "Audit log"},
    "Réception régionale": {"en": "Regional receiving"},
    "Inventaire régional": {"en": "Regional inventory"},
    "Alertes régionales": {"en": "Regional alerts"},
    "Retours équipement": {"en": "Equipment returns"},
    "Historique régional": {"en": "Regional history"},
    "Transfert entre régions": {"en": "Inter-region transfer"},
    "État du système": {"en": "System status"},
    "Jaugeage": {"en": "Gauge reading"},
    "WH Central": {"en": "WH Central"},
    "⏻  Déconnexion": {"en": "⏻  Sign out"},
    "🔑  Mon mot de passe": {"en": "🔑  My password"},
    "⚙  Paramètres": {"en": "⚙  Settings"},

    # ------------------------------------------------------------- rôles
    "Administrateur": {"en": "Administrator"},
    "Magasinier": {"en": "Warehouse operator"},
    "Lecteur": {"en": "Read-only"},
    "Entrepôt régional": {"en": "Regional warehouse"},

    # ------------------------------------------- file d'attente / temps réel
    "Bons en attente": {"en": "Pending documents"},
    "File d'attente": {"en": "Pending queue"},
    "Session expirée": {"en": "Session expired"},
    "Ta session a expiré. Reconnecte-toi.":
        {"en": "Your session has expired. Please sign in again."},
    "⚠  Temps réel interrompu — cliquer pour relancer":
        {"en": "⚠  Live updates interrupted — click to restart"},
    "Bons refusés par le serveur": {"en": "Documents rejected by the server"},
    "Bon": {"en": "Document"},
    "Ajustement": {"en": "Adjustment"},
    "sans tiers": {"en": "no party"},
    "un produit": {"en": "a product"},
    "une région": {"en": "a region"},
    "{n} bon(s) en file d'attente n'ont pas encore été envoyés.\n\n"
    "Si tu fermes maintenant, ils seront renvoyés au prochain "
    "lancement.\n\nFermer quand même ?":
        {"en": "{n} document(s) in the queue have not been sent yet.\n\n"
               "If you close now, they will be sent again at the next "
               "launch.\n\nClose anyway?"},
    "  ⏳ {n} bon(s) en attente  ": {"en": "  ⏳ {n} document(s) pending  "},
    "  ✓ {n} bon(s) en attente envoyé(s)  ":
        {"en": "  ✓ {n} pending document(s) sent  "},
    "Total global : {n} bon(s) en attente d'envoi, tous types confondus "
    "(une seule file partagée par les trois onglets).":
        {"en": "Overall total: {n} document(s) waiting to be sent, all types "
               "combined (a single queue shared by the three tabs)."},
    "{n} bon(s) en attente n'ont pas pu être enregistrés et ont "
    "été retirés de la file :\n\n{details}\n\nRessaisis-les si "
    "nécessaire.":
        {"en": "{n} pending document(s) could not be saved and were removed "
               "from the queue:\n\n{details}\n\nRe-enter them if needed."},
    "  ... et {n} autre(s)": {"en": "  ... and {n} more"},
    "{n} bon(s) en attente d'envoi au serveur :":
        {"en": "{n} document(s) waiting to be sent to the server:"},
    "Ils seront renvoyés automatiquement quand le serveur sera disponible.":
        {"en": "They will be sent again automatically once the server is "
               "available."},
    "⚠ {nom} sous son seuil": {"en": "⚠ {nom} below its threshold"},
    "⚠ {region} signale une rupture : {nom}":
        {"en": "⚠ {region} reports a stock-out: {nom}"},

    # ------------------------------------------------------ tableau de bord
    "Vue d'ensemble": {"en": "Overview"},
    "Activité du jour": {"en": "Today's activity"},
    "Alertes de stock": {"en": "Stock alerts"},
    "Mouvements récents": {"en": "Recent movements"},
    "Réceptions du jour": {"en": "Receipts today"},
    "Expéditions du jour": {"en": "Shipments today"},
    "Produits en alerte": {"en": "Products in alert"},
    "Produits en rupture": {"en": "Out-of-stock products"},
    "Valeur du stock": {"en": "Stock value"},
    "Aucun mouvement aujourd'hui": {"en": "No movement today"},
    "Aucune alerte": {"en": "No alerts"},
    "Rupture": {"en": "Out of stock"},
    "Critique": {"en": "Critical"},
    "Alerte": {"en": "Alert"},
    "Normal": {"en": "Normal"},
    "Actions rapides": {"en": "Quick actions"},
    "Nouvelle réception": {"en": "New receipt"},
    "Nouvelle expédition": {"en": "New shipment"},
    "Voir l'historique": {"en": "View history"},
    "Unités en stock": {"en": "Units in stock"},
    "Références": {"en": "SKUs"},
    "Alertes stock": {"en": "Stock alerts"},
    "Couverture < 7j": {"en": "Coverage < 7d"},
    "Reçu (30 j)": {"en": "Received (30 d)"},
    "Livré (30 j)": {"en": "Shipped (30 d)"},
    "Retours (30 j)": {"en": "Returns (30 d)"},
    "Bons (30 j)": {"en": "Documents (30 d)"},
    "Rotation (30 j)": {"en": "Turnover (30 d)"},
    "Stock par catégorie": {"en": "Stock by category"},
    "Livraisons par région (30 j)": {"en": "Deliveries by region (30 d)"},
    "Top 5 des sorties (30 j)": {"en": "Top 5 outbound (30 d)"},
    "livré ÷ stock actuel": {"en": "shipped ÷ current stock"},
    "Écarts de stock": {"en": "Stock discrepancies"},
    "Écarts de stock détectés": {"en": "Stock discrepancies detected"},
    "Rapport": {"en": "Report"},
    "Rapport mensuel": {"en": "Monthly report"},
    "← Retour": {"en": "← Back"},
    "Conso/jour": {"en": "Usage/day"},
    "Jours restants": {"en": "Days left"},
    "Tous > 7 jours": {"en": "All > 7 days"},
    "●  En direct": {"en": "●  Live"},
    "●  Hors ligne — tentative dans {s}s":
        {"en": "●  Offline — retrying in {s}s"},
    "RUPTURE": {"en": "OUT"},
    "CRITIQUE": {"en": "CRITICAL"},
    "BAS": {"en": "LOW"},
    "Données non disponibles. Attends le chargement.":
        {"en": "Data not available yet. Please wait for it to load."},
    "Résumé des 30 derniers jours": {"en": "Summary of the last 30 days"},
    "Signature responsable": {"en": "Manager signature"},
    "cliquer une barre pour le détail":
        {"en": "click a bar for the breakdown"},
    "{categorie} — {unites} unités, {refs} réf.":
        {"en": "{categorie} — {unites} units, {refs} SKUs"},
    "{n} produit(s)": {"en": "{n} product(s)"},
    "{n} en rupture": {"en": "{n} out of stock"},
    "{n} affichés": {"en": "{n} shown"},
    "{n} référence(s) sans coût": {"en": "{n} SKU(s) without a cost"},
    "{pct} vs mois dernier": {"en": "{pct} vs last month"},
    "Tout OK": {"en": "All clear"},
    "Total reçu": {"en": "Total received"},
    "Total livré": {"en": "Total shipped"},
    "Total retours": {"en": "Total returns"},
    "Nombre de bons": {"en": "Number of documents"},
    "Références en stock": {"en": "SKUs in stock"},
    "État": {"en": "Status"},
    "Aucune alerte — tous les stocks sont au-dessus des seuils.":
        {"en": "No alerts — every stock level is above its threshold."},
    "Rapport mensuel exporté vers {path}":
        {"en": "Monthly report exported to {path}"},
    "⚠  À réapprovisionner": {"en": "⚠  To restock"},
    "📉  Couverture de stock (jours restants)":
        {"en": "📉  Stock coverage (days remaining)"},
    "Rapport mensuel — {mois} {annee}": {"en": "Monthly report — {mois} {annee}"},
    "Généré le {date} — WMS Entrepôt":
        {"en": "Generated on {date} — WMS Warehouse"},
    "Top {n} des produits les plus sortis (30 j)":
        {"en": "Top {n} most shipped products (30 d)"},
    "Rapport d'activité — 30 derniers jours":
        {"en": "Activity report — last 30 days"},
    "Période couverte : {periode}": {"en": "Period covered: {periode}"},
    "du {d1} au {d2}": {"en": "from {d1} to {d2}"},
    "Rapport 30 jours — généré le {date}":
        {"en": "30-day report — generated on {date}"},
    "Alertes stock ({n} produit(s))": {"en": "Stock alerts ({n} product(s))"},
    "Stock enregistré": {"en": "Recorded stock"},
    "Stock recalculé": {"en": "Recalculated stock"},
    "Stock produit": {"en": "Product stock"},
    "Stock entrepôt (calculé)": {"en": "Warehouse stock (calculated)"},
    "⚠  {n} écart(s) de stock détecté(s) — cliquer pour le détail":
        {"en": "⚠  {n} stock discrepancy(ies) detected — click for details"},
    "{n} premiers sur {total} — voir la vue Produits pour la liste complète":
        {"en": "First {n} of {total} — see the Products view for the full list"},
    "{n} premiers sur {total} produits en alerte.":
        {"en": "First {n} of {total} products in alert."},
    "Aucun écart — le grand livre est cohérent.":
        {"en": "No discrepancy — the ledger is consistent."},
    "Stock enregistré ≠ stock recalculé depuis les mouvements":
        {"en": "Recorded stock ≠ stock recalculated from movements"},
    "Stock entrepôt (par emplacement) ≠ stock produit":
        {"en": "Warehouse stock (by location) ≠ product stock"},

    # ----------------------------------------------------------- mois (PDF)
    "Janvier": {"en": "January"},
    "Février": {"en": "February"},
    "Mars": {"en": "March"},
    "Avril": {"en": "April"},
    "Mai": {"en": "May"},
    "Juin": {"en": "June"},
    "Juillet": {"en": "July"},
    "Août": {"en": "August"},
    "Septembre": {"en": "September"},
    "Octobre": {"en": "October"},
    "Novembre": {"en": "November"},
    "Décembre": {"en": "December"},

    # --------------------------------------------------------------- stock
    "Stock actuel": {"en": "Current stock"},
    "Stock disponible": {"en": "Available stock"},
    "Stock minimum": {"en": "Minimum stock"},
    "Rechercher un produit...": {"en": "Search for a product..."},
    "Aucun produit": {"en": "No products"},
    "Exporter en Excel": {"en": "Export to Excel"},
    "📦  Stock en temps réel": {"en": "📦  Live stock"},
    "Stock en temps réel — {date}": {"en": "Live stock — {date}"},
    "📄  Export PDF": {"en": "📄  Export PDF"},
    "📥  Exporter Excel": {"en": "📥  Export Excel"},
    "📋  Réappro": {"en": "📋  Restock"},
    "Export": {"en": "Export"},
    "Export PDF": {"en": "PDF export"},
    "Seuil min": {"en": "Min. threshold"},
    "🔍  Rechercher un produit...": {"en": "🔍  Search for a product..."},
    "Dernière maj : {heure}": {"en": "Last update: {heure}"},
    "Serveur injoignable — nouvelle tentative dans {s} s "
    "(affichage : dernière synchronisation connue)":
        {"en": "Server unreachable — retrying in {s} s "
               "(showing: last known synchronisation)"},
    "File d'attente synchronisée": {"en": "Queue synchronised"},
    "{n} bon(s) en attente envoyé(s) avec succès.":
        {"en": "{n} pending document(s) sent successfully."},
    "{n} rejeté(s) par le serveur :\n{details}":
        {"en": "{n} rejected by the server:\n{details}"},
    "{shown} / {total} produits": {"en": "{shown} / {total} products"},
    "{n} produits": {"en": "{n} products"},
    "{n} critique(s)": {"en": "{n} critical"},
    "{n} bas": {"en": "{n} low"},
    "⚠ Stock : {parties}": {"en": "⚠ Stock: {parties}"},
    "Tous les stocks sont normaux.": {"en": "All stock levels are normal."},
    "{n} produit(s) exporté(s) vers {path}":
        {"en": "{n} product(s) exported to {path}"},
    "Stock exporté vers {path}": {"en": "Stock exported to {path}"},
    "Rapport de réapprovisionnement exporté vers {path}":
        {"en": "Restocking report exported to {path}"},
    "{n} produit(s) — Généré le {date}":
        {"en": "{n} product(s) — Generated on {date}"},
    "Le module fpdf2 n'est pas installé.\nInstallez-le avec : pip install fpdf2":
        {"en": "The fpdf2 module is not installed.\n"
               "Install it with: pip install fpdf2"},
    "Impossible d'écrire le fichier :\n{e}\n\nVérifie qu'il n'est pas déjà "
    "ouvert dans un autre programme.":
        {"en": "Could not write the file:\n{e}\n\nCheck that it is not "
               "already open in another program."},

    # ------------------------------------------------------------ documents
    "Numéro": {"en": "Number"},
    "Numéro de bon": {"en": "Document number"},
    "Lignes": {"en": "Lines"},
    "Ajouter une ligne": {"en": "Add a line"},
    "Supprimer la ligne": {"en": "Delete line"},
    "Enregistrer le bon": {"en": "Save document"},
    "Bon enregistré": {"en": "Document saved"},
    "Annuler le bon": {"en": "Cancel document"},
    "Bon annulé": {"en": "Document cancelled"},
    "Aucune ligne": {"en": "No lines"},
    "Le bon doit contenir au moins une ligne":
        {"en": "The document must contain at least one line"},
    # TYPE_LABELS (document_form) — traduits au moment de l'affichage
    "Source": {"en": "Source"},
    "Destination": {"en": "Destination"},
    "Réceptionnaire": {"en": "Recipient"},
    "Provenance": {"en": "Origin"},
    "Superviseur": {"en": "Supervisor"},
    "N° Bordereau": {"en": "Slip no."},
    "N° Reçu": {"en": "Receipt no."},
    "N° Retour": {"en": "Return no."},
    "✓  Enregistrer la réception": {"en": "✓  Save receipt"},
    "✓  Enregistrer l'expédition": {"en": "✓  Save shipment"},
    "✓  Enregistrer le retour": {"en": "✓  Save return"},
    # « Aucun » (AUCUN_TECHNICIEN) est déjà défini plus haut, section générale.
    "bidons": {"en": "drums"},
    "Transporté par": {"en": "Carried by"},
    "Technicien (facultatif)": {"en": "Technician (optional)"},
    "ex: 10": {"en": "e.g. 10"},
    "+  Ajouter": {"en": "+  Add"},
    "🗑  Retirer la ligne": {"en": "🗑  Remove line"},
    "🔄  Nouveau bon vierge": {"en": "🔄  New blank document"},
    "{sku} - {nom} (stock: {stock:g} {unite})":
        {"en": "{sku} - {nom} (stock: {stock:g} {unite})"},
    "Ce superviseur est rattaché à plusieurs régions : sélectionne la région.":
        {"en": "This supervisor covers several regions: select the region."},
    "Serveur injoignable et aucun catalogue en mémoire — impossible de saisir "
    "pour l'instant":
        {"en": "Server unreachable and no catalogue in memory — data entry is "
               "not possible right now"},
    "Mode hors ligne — catalogue en cache":
        {"en": "Offline mode — cached catalogue"},
    "Bon en cours retrouvé": {"en": "Unfinished document found"},
    "Bon non terminé {origine} — {nb} ligne(s).\n\nLe reprendre ?":
        {"en": "Unfinished document {origine} — {nb} line(s).\n\nResume it?"},
    "du {quand}": {"en": "from {quand}"},
    "par {auteur}": {"en": "by {auteur}"},
    "retrouvé sur ce poste": {"en": "found on this station"},
    "Bon repris — vérifie les quantités avant d'enregistrer":
        {"en": "Document resumed — check the quantities before saving"},
    "Sélectionne un produit valide": {"en": "Select a valid product"},
    "Aucun produit ne correspond à « {saisie} »": {
        "en": "No product matches “{saisie}”",
        "ht": "Pa gen pwodwi ki koresponn ak « {saisie} »"},
    "Quantité invalide": {"en": "Invalid quantity"},
    "Stock insuffisant : {demande:g} {unite} demandés au total sur ce bon, "
    "{dispo:g} {unite} disponibles":
        {"en": "Not enough stock: {demande:g} {unite} requested in total on "
               "this document, {dispo:g} {unite} available"},
    "Quantité fusionnée avec la ligne existante":
        {"en": "Quantity merged into the existing line"},
    "{qty:g} {unite} ({bidons:g} bid.)":
        {"en": "{qty:g} {unite} ({bidons:g} drums)"},
    "{n} ligne(s) — {total:g} unités": {"en": "{n} line(s) — {total:g} units"},
    "Vider le formulaire": {"en": "Clear the form"},
    "Un bon est en cours avec {n} ligne(s).\n\nTout effacer ?":
        {"en": "A document is in progress with {n} line(s).\n\nClear "
               "everything?"},
    "Nouveau bon vierge": {"en": "New blank document"},
    "Le bon en cours contient des lignes non enregistrées.\n\n"
    "Tout effacer et repartir d'un bon vide ?":
        {"en": "The current document has unsaved lines.\n\nClear everything "
               "and start from a blank document?"},
    "Date ancienne": {"en": "Old date"},
    "La date saisie est le {date}.\n\nCette date est ancienne "
    "(plus de 7 jours) — confirmer ?":
        {"en": "The date entered is {date}.\n\nThat date is old (more than "
               "7 days) — confirm?"},
    "Ajoute au moins une ligne avant de valider":
        {"en": "Add at least one line before submitting"},
    "Confirmer la réception": {"en": "Confirm receipt", "ht": "Konfime resepsyon an"},
    "Réception de {nb} produit(s) — {total:g} unité(s) au total.\n\n"
    "Confirmer l'enregistrement ?":
        {"en": "Receipt of {nb} product(s) — {total:g} unit(s) in total.\n\n"
               "Confirm saving?"},
    "une expédition": {"en": "a shipment"},
    "un retour": {"en": "a return"},
    "La région est obligatoire pour {quoi} (elle rattache le mouvement à un "
    "superviseur)":
        {"en": "The region is required for {quoi} (it links the movement to a "
               "supervisor)"},
    "Date invalide (format attendu : AAAA-MM-JJ HH:MM)":
        {"en": "Invalid date (expected format: YYYY-MM-DD HH:MM)"},
    "La date ne peut pas être dans le futur":
        {"en": "The date cannot be in the future"},
    "Corrige la date du bon avant de valider":
        {"en": "Correct the document date before submitting"},
    "Confirmer l'expédition": {"en": "Confirm shipment"},
    "Confirmer le retour": {"en": "Confirm return"},
    "Tu vas enregistrer une expédition :\n\n"
    "  {nb} ligne(s), {total:g} unité(s) au total\n"
    "  Région : {region}\n"
    "  Réceptionnaire : {party}\n"
    "  Technicien : {technicien}\n\n"
    "Confirmer ?":
        {"en": "You are about to save a shipment:\n\n"
               "  {nb} line(s), {total:g} unit(s) in total\n"
               "  Region: {region}\n"
               "  Recipient: {party}\n"
               "  Technician: {technicien}\n\n"
               "Confirm?"},
    "Tu vas enregistrer un retour :\n\n"
    "  {nb} ligne(s), {total:g} unité(s) au total\n"
    "  Région d'origine : {region}\n"
    "  Provenance : {party}\n\n"
    "Les produits seront remis en stock entrepôt.\n"
    "Confirmer ?":
        {"en": "You are about to save a return:\n\n"
               "  {nb} line(s), {total:g} unit(s) in total\n"
               "  Origin region: {region}\n"
               "  Origin: {party}\n\n"
               "The products will be put back into warehouse stock.\n"
               "Confirm?"},
    "Envoi en cours...": {"en": "Sending..."},
    "Patiente...": {"en": "Please wait..."},
    "Bon déjà enregistré": {"en": "Document already saved"},
    "{type} n°{id} était déjà enregistrée (la réponse du premier envoi "
    "s'était perdue).\n\nAucun doublon n'a été créé, le stock n'a été modifié "
    "qu'une fois.":
        {"en": "{type} no. {id} had already been saved (the first response "
               "was lost).\n\nNo duplicate was created; stock was changed "
               "only once."},
    "✓ {type} n°{id} enregistré(e) — {n} ligne(s)":
        {"en": "✓ {type} no. {id} saved — {n} line(s)"},
    "Attention — produits sous le seuil :":
        {"en": "Warning — products below threshold:"},
    "     Stock : {stock:g}  /  Seuil : {seuil:g}":
        {"en": "     Stock: {stock:g}  /  Threshold: {seuil:g}"},
    "Alerte seuil": {"en": "Threshold alert"},
    "Vérification à faire": {"en": "Check required"},
    "Le bon est enregistré, mais :": {"en": "The document was saved, but:"},
    "Impression": {"en": "Printing"},
    "{type} n°{id} enregistrée.\n\nImprimer ce bon maintenant ?":
        {"en": "{type} no. {id} saved.\n\nPrint this document now?"},
    "Ne plus demander sur ce poste":
        {"en": "Don't ask again on this station"},
    "Le bon est bien enregistré, mais le PDF n'a pas pu être créé :\n{e}":
        {"en": "The document was saved, but the PDF could not be "
               "created:\n{e}"},
    "Serveur injoignable": {"en": "Server unreachable", "ht": "Sèvè a pa reponn"},
    "Le serveur ne répond pas.\n\nMettre ce bon en file d'attente ?\n"
    "Il sera envoyé automatiquement au retour du réseau.":
        {"en": "The server is not responding.\n\nQueue this document?\n"
               "It will be sent automatically once the network is back."},
    "⚠ Attention : le stock n'a pas été vérifié par le serveur.":
        {"en": "⚠ Warning: stock has not been checked by the server."},
    "En file d'attente": {"en": "Queued"},
    "Bon mis en attente ({n} en file).\nIl sera envoyé dès que le serveur "
    "sera joignable.":
        {"en": "Document queued ({n} in queue).\nIt will be sent as soon as "
               "the server is reachable."},
    "Nouveau {type} enregistré (n°{id})":
        {"en": "New {type} saved (no. {id})"},
    "{type} annulé (n°{id})": {"en": "{type} cancelled (no. {id})"},

    # ------------------------------------------------------------- historique
    "🕐  Historique des mouvements": {"en": "🕐  Movement history"},
    "Historique des mouvements": {"en": "Movement history"},
    "↻  Rafraîchir": {"en": "↻  Refresh", "ht": "↻  Aktyalize"},
    "Action sur le bon sélectionné :": {"en": "Action on the selected document:"},
    "Dupliquer": {"en": "Duplicate"},
    "Duplication": {"en": "Duplication"},
    "Réceptions": {"en": "Receipts"},
    "Expéditions": {"en": "Shipments"},
    "Retours": {"en": "Returns"},
    "Ajustements": {"en": "Adjustments"},
    "Inventaires": {"en": "Inventories"},
    "Tous les produits": {"en": "All products"},
    "🔍  SKU / produit / réf...": {"en": "🔍  SKU / product / ref..."},
    "Filtrer par date": {"en": "Filter by date"},
    "Du": {"en": "From"},
    "  Au": {"en": "  To"},
    "Page suivante ›": {"en": "Next page ›"},
    "‹ Page précédente": {"en": "‹ Previous page"},
    "Validé": {"en": "Validated"},
    "Reçu": {"en": "Received", "ht": "Resevwa"},
    "En transit": {"en": "In transit"},
    "ANNULÉ": {"en": "CANCELLED"},
    "BON ANNULÉ": {"en": "DOCUMENT CANCELLED"},
    "Bons {debut} à {fin} sur {total}":
        {"en": "Documents {debut} to {fin} of {total}"},
    "Aucun mouvement": {"en": "No movement"},
    " — du {d1} au {d2}": {"en": " — from {d1} to {d2}"},
    "[Transporteur : {carrier}]": {"en": "[Carrier: {carrier}]"},
    "[Annulé : {motif}]": {"en": "[Cancelled: {motif}]"},
    "sans motif": {"en": "no reason given"},
    "Transporteur": {"en": "Carrier"},
    "Note :": {"en": "Note:"},
    "Sélectionne une ligne pour annuler son bon.":
        {"en": "Select a line to cancel its document."},
    "Sélectionne les lignes d'un seul bon à la fois.":
        {"en": "Select lines from a single document at a time."},
    "Le bon n°{id} est déjà annulé.":
        {"en": "Document no. {id} is already cancelled."},
    "Annuler le bon n°{id} sélectionné.":
        {"en": "Cancel the selected document no. {id}."},
    "Annulation": {"en": "Cancellation"},
    "Annulation impossible": {"en": "Cancellation failed"},
    "Sélectionne d'abord une ligne du bon à annuler.":
        {"en": "First select a line of the document to cancel."},
    "La sélection porte sur plusieurs bons différents.\n"
    "Sélectionne des lignes d'un seul bon à la fois.":
        {"en": "The selection spans several different documents.\n"
               "Select lines from a single document at a time."},
    "{type} n°{id} annulée.\nLe stock a été rétabli et le bon reste tracé "
    "dans l'historique.":
        {"en": "{type} no. {id} cancelled.\nStock has been restored and the "
               "document remains recorded in the history."},
    "Annulation de {type} n°{id}.\nLe mouvement de stock sera inversé.\n\n"
    "Motif (obligatoire, 3 caractères minimum) :":
        {"en": "Cancelling {type} no. {id}.\nThe stock movement will be "
               "reversed.\n\nReason (required, 3 characters minimum):"},
    "Motif trop court": {"en": "Reason too short"},
    "Le motif doit faire au moins 3 caractères.":
        {"en": "The reason must be at least 3 characters long."},
    "Ce bon est déjà annulé.": {"en": "This document is already cancelled."},
    "Seuls les bons de type réception, expédition ou retour peuvent être "
    "dupliqués.":
        {"en": "Only receiving, shipping and return documents can be "
               "duplicated."},
    "PDF": {"en": "PDF"},
    "Réf": {"en": "Ref"},
    "Date du bon : {date}": {"en": "Document date: {date}"},
    "Total ({n} ligne(s))": {"en": "Total ({n} line(s))"},
    "Opérateur : _______________": {"en": "Operator: _______________"},
    "Superviseur : _______________": {"en": "Supervisor: _______________"},
    "Imprimé le {date} à {heure}": {"en": "Printed on {date} at {heure}"},
    "Page {n}/{total}": {"en": "Page {n}/{total}"},
    "Bon n°{id} exporté vers {path}":
        {"en": "Document no. {id} exported to {path}"},
    "Exporté le {date} — {n} ligne(s) affichée(s) sur {total} bon(s) au total":
        {"en": "Exported on {date} — {n} row(s) shown out of {total} "
               "document(s) in total"},
    "L'export reprend le type, le produit et la période, mais PAS {filtres} : "
    "le fichier contiendra plus de lignes que le tableau affiché.\n\n"
    "Continuer ?":
        {"en": "The export keeps the type, product and period filters, but "
               "NOT {filtres}: the file will contain more rows than the table "
               "on screen.\n\nContinue?"},
    "la recherche « {q} »": {"en": "the search “{q}”"},
    "la personne « {op} »": {"en": "the person “{op}”"},
    " ni ": {"en": " nor "},
    "Export Excel": {"en": "Excel export"},
    "PDF exporté vers {path}": {"en": "PDF exported to {path}"},
    "Historique exporté vers {path}": {"en": "History exported to {path}"},
    "Sélectionne une ligne pour l'imprimer.":
        {"en": "Select a line to print it."},
    "Imprimer le bon n°{id} sélectionné.":
        {"en": "Print the selected document no. {id}."},
    # --- filtres FUEL historique
    "Jaugeages": {"en": "Gauge readings"},
    "Transferts régionaux": {"en": "Regional transfers"},
    "Retours fournisseur": {"en": "Supplier returns"},
    # --- type labels historique
    "Transfert régional": {"en": "Regional transfer"},
    "Retour fournisseur": {"en": "Supplier return"},
    # --- en-têtes colonnes FUEL historique
    "Cuve": {"en": "Tank"},
    "Qté (gls)": {"en": "Qty (gal)"},
    "N° Bon": {"en": "Doc #"},
    "Receveur": {"en": "Receiver"},
    "Plaque": {"en": "Plate"},
    "Véhicule": {"en": "Vehicle"},
    "Projet": {"en": "Project"},
    "Km": {"en": "Mi"},
    "Carte carburant": {"en": "Fuel card"},
    "Commentaires": {"en": "Comments"},
    # --- en-têtes colonnes non-FUEL historique (anglais→français)
    "N° Pièce": {"en": "Part Number"},
    "Date livraison": {"en": "Delivery Date"},

    # ------------------------------------------------------------ inventaire
    "Écart": {"en": "Variance", "ht": "Diferans"},
    "Stock compté": {"en": "Counted stock"},
    "Ajuster": {"en": "Adjust"},
    "Ajustement enregistré": {"en": "Adjustment saved"},
    "Motif :": {"en": "Reason:"},
    "Motif": {"en": "Reason"},
    "📋  Inventaire physique": {"en": "📋  Physical inventory"},
    "Lance un inventaire pour compter tous les produits de l'entrepôt.":
        {"en": "Start an inventory to count every product in the warehouse."},
    "Opérateur :": {"en": "Operator:"},
    "Date :": {"en": "Date:"},
    "Date : {date}": {"en": "Date: {date}"},
    "Opérateur : {nom}": {"en": "Operator: {nom}"},
    "▶  Démarrer l'inventaire": {"en": "▶  Start inventory"},
    "✓  Terminer l'inventaire": {"en": "✓  Finish inventory"},
    "✕  Annuler": {"en": "✕  Cancel"},
    "🔍  Filtrer les produits...": {"en": "🔍  Filter products..."},
    # Douchette / codes-barres (inventaire et fiche produit)
    "🔦  Scanne un code-barres, puis Entrée":
        {"en": "🔦  Scan a barcode, then press Enter",
         "ht": "🔦  Eskane yon kòd, apre peze Antre"},
    "Démarre l'inventaire avant de scanner.":
        {"en": "Start the inventory before scanning.",
         "ht": "Kòmanse envantè a anvan ou eskane."},
    "Recherche du code {code}…": {"en": "Looking up code {code}…"},
    "{sku} — {nom}": {"en": "{sku} — {nom}"},
    "« {nom} » n'est pas dans ce comptage (produit archivé ou ajouté après le "
    "démarrage).":
        {"en": "« {nom} » is not part of this count (product archived or added "
               "after the start)."},
    "Code inconnu : {code}": {"en": "Unknown code: {code}",
                              "ht": "Kòd enkoni : {code}"},
    "Code-barres inconnu": {"en": "Unknown barcode"},
    "Aucun produit ne porte le code « {code} ».\n\nAssocier ce code à un "
    "produit existant maintenant ?\n\n« Non » : tu peux aussi le faire plus "
    "tard depuis l'écran Produits.":
        {"en": "No product carries the code « {code} ».\n\nLink this code to "
               "an existing product now?\n\n« No »: you can also do it later "
               "from the Products screen."},
    "Associer le code-barres": {"en": "Link the barcode"},
    "{n} résultats affichés sur {total} — affine ta recherche pour voir les autres.":
        {"en": "{n} results shown out of {total} — narrow your search to see the rest."},
    "Code scanné : {code}": {"en": "Scanned code: {code}"},
    "Choisis le produit qui porte cette étiquette. Le code lui sera "
    "enregistré définitivement.":
        {"en": "Pick the product carrying this label. The code will be saved "
               "on it permanently."},
    "🔍  Chercher un produit (SKU ou nom)…":
        {"en": "🔍  Search a product (SKU or name)…"},
    "Associer": {"en": "Link"},
    "Choisis d'abord un produit dans la liste.":
        {"en": "Pick a product from the list first."},
    "Code-barres :": {"en": "Barcode:"},
    "Code-barres (facultatif — scanne-le ici)":
        {"en": "Barcode (optional — scan it here)"},
    "vide = aucun": {"en": "empty = none"},
    "Compté": {"en": "Counted", "ht": "Konte"},
    "Comptage": {"en": "Count"},
    "Aucun produit en base.": {"en": "No products in the database."},
    "Double-clique sur une ligne pour saisir le comptage.":
        {"en": "Double-click a row to enter the count."},
    "(1 bidon = {cap:g} {unite})": {"en": "(1 drum = {cap:g} {unite})"},
    "{sku} — {nom}{rappel}\n\nQuantité comptée (en {unite}) :":
        {"en": "{sku} — {nom}{rappel}\n\nCounted quantity (in {unite}):"},
    "Valeur invalide": {"en": "Invalid value"},
    "Saisis un nombre positif ou zéro.":
        {"en": "Enter a positive number or zero."},
    "{comptes} / {total} produits comptés":
        {"en": "{comptes} / {total} products counted"},
    "Annuler l'inventaire": {"en": "Cancel inventory"},
    "Abandonner cet inventaire en cours ?\n"
    "Les comptages saisis seront perdus.":
        {"en": "Abandon this inventory in progress?\n"
               "The counts entered will be lost."},
    "Aucun produit n'a été compté.": {"en": "No product was counted."},
    "Produits non comptés": {"en": "Uncounted products"},
    "{n} produit(s) n'ont pas été comptés.\n\nLes terminer quand même ? "
    "Seuls les produits comptés seront ajustés.":
        {"en": "{n} product(s) were not counted.\n\nFinish anyway? Only "
               "counted products will be adjusted."},
    "Inventaire terminé": {"en": "Inventory complete"},
    "{n} produit(s) comptés — aucun écart constaté.\n\n"
    "Le stock correspond au comptage physique.":
        {"en": "{n} product(s) counted — no discrepancy found.\n\n"
               "Stock matches the physical count."},
    "Confirmer l'inventaire": {"en": "Confirm inventory"},
    "{n} produit(s) comptés, {ecarts} avec écart.\n\n"
    "Manquant : {manques:g}\nSurplus : +{surplus:g}\n\n"
    "{action}":
        {"en": "{n} product(s) counted, {ecarts} with a discrepancy.\n\n"
               "Missing: {manques:g}\nSurplus: +{surplus:g}\n\n"
               "{action}"},
    "Le stock sera corrigé. Continuer ?":
        {"en": "Stock will be corrected. Continue?"},
    "Le relevé sera enregistré (stock non corrigé — "
    "seul l'administrateur peut corriger le stock). "
    "Continuer ?":
        {"en": "The reading will be recorded (stock not corrected — "
               "only the administrator can correct stock). "
               "Continue?"},
    "Écart important détecté": {"en": "Large discrepancy detected"},
    "Cet écart est anormalement élevé.\nConfirmez-vous ce comptage ?":
        {"en": "This discrepancy is unusually large.\nDo you confirm this "
               "count?"},
    "Le serveur ne répond pas.\n\nMettre cet inventaire en file d'attente ?":
        {"en": "The server is not responding.\n\nQueue this inventory?"},
    "Impossible de charger les produits :\n{e}":
        {"en": "Could not load the products:\n{e}"},
    "Rapport d'inventaire": {"en": "Inventory report"},
    "Inventaire n°{id} enregistré": {"en": "Inventory no. {id} saved"},
    "Produits comptés : {n}": {"en": "Products counted: {n}"},
    "Produits non comptés : {n}": {"en": "Products not counted: {n}"},
    "Écarts constatés : {n}": {"en": "Discrepancies found: {n}"},
    "MANQUES ({n}) :": {"en": "SHORTAGES ({n}):"},
    "SURPLUS ({n}) :": {"en": "SURPLUSES ({n}):"},
    "ALERTES SEUIL ({n}) :": {"en": "THRESHOLD ALERTS ({n}):"},
    "stock {stock:g} / seuil {seuil:g}":
        {"en": "stock {stock:g} / threshold {seuil:g}"},
    "Exporter Excel": {"en": "Export Excel"},
    "N° document : {id}": {"en": "Document no.: {id}"},
    "Non comptés : {n}": {"en": "Not counted: {n}"},
    "Écarts : {n}": {"en": "Discrepancies: {n}"},
    "Stock théorique": {"en": "Theoretical stock"},
    "Manque": {"en": "Shortage"},
    "Surplus": {"en": "Surplus"},
    "Rapport exporté vers :\n{path}": {"en": "Report exported to:\n{path}"},
    "Impossible d'enregistrer :\n{e}": {"en": "Could not save:\n{e}"},

    # ------------------------------------------------- rapport de jaugeage
    "Rapport de jaugeage": {"en": "Gauging report"},
    "{document} (suite)": {"en": "{document} (continued)"},
    "Jaugeage n°{id} enregistré": {"en": "Gauging no. {id} saved"},
    "Cuves relevées : {n}": {"en": "Tanks read: {n}"},
    "Cuves non relevées : {n}": {"en": "Tanks not read: {n}"},
    "NIVEAU EN BAISSE ({n}) :": {"en": "LEVEL DOWN ({n}):"},
    "NIVEAU EN HAUSSE ({n}) :": {"en": "LEVEL UP ({n}):"},
    "écart normal — jauge/évaporation": {"en": "normal reading — gauge/evaporation"},
    "écart normal": {"en": "normal discrepancy"},
    "⚠ écart important — vérifié en double saisie":
        {"en": "⚠ significant discrepancy — verified by double entry"},
    "Niveau théorique": {"en": "Theoretical level"},
    "Niveau relevé": {"en": "Level read"},
    "Ampleur": {"en": "Extent"},

    # --------------------------------------------------------------- produits
    "🗄  Catalogue produits": {"en": "🗄  Product catalogue"},
    "📥  Importer Excel": {"en": "📥  Import Excel"},
    "🔍  Rechercher...": {"en": "🔍  Search..."},
    "Ajouter un produit": {"en": "Add a product"},
    "SKU *": {"en": "SKU *"},
    "Nom *": {"en": "Name *"},
    "SKU :": {"en": "SKU:"},
    "Nom :": {"en": "Name:"},
    "Unité :": {"en": "Unit:"},
    "Pièce": {"en": "Piece"},
    "Volume (gls)": {"en": "Volume (gls)"},
    "Gls/bidon": {"en": "Gls/drum"},
    "Gls/bidon :": {"en": "Gls/drum:"},
    "Coût unitaire": {"en": "Unit cost"},
    "Coût unitaire :": {"en": "Unit cost:"},
    "Consigne bouteille\n(gallons/bouteille) :":
        {"en": "Bottle deposit\n(gallons/bottle):"},
    "vide = aucune": {"en": "empty = none"},
    "vide = non renseigné": {"en": "empty = not set"},
    "Le stock affiché est en lecture seule ici : il se modifie via "
    "Réception / Expédition / Inventaire.":
        {"en": "Stock shown here is read-only: it changes through "
               "Receiving / Shipping / Inventory."},
    "Archivé": {"en": "Archived"},
    "Archiver": {"en": "Archive"},
    "Réactiver": {"en": "Reactivate"},
    "Afficher les archivés": {"en": "Show archived"},
    "Archiver le produit": {"en": "Archive product"},
    "Réactiver le produit": {"en": "Reactivate product"},
    "Archivage en cours…": {"en": "Archiving…"},
    "Réactivation en cours…": {"en": "Reactivating…"},
    "Envoi…": {"en": "Sending…", "ht": "Ap voye…"},
    "Création en cours…": {"en": "Creating…"},
    "Enregistrement…": {"en": "Saving…", "ht": "Ap anrejistre…"},
    "Import en cours…": {"en": "Importing…"},
    "Import en cours, patiente…": {"en": "Import in progress, please wait…"},
    "Import terminé": {"en": "Import complete"},
    "Accès refusé": {"en": "Access denied"},
    "Seul un administrateur peut modifier un produit.":
        {"en": "Only an administrator can edit a product."},
    "SKU et nom sont obligatoires": {"en": "SKU and name are required"},
    "SKU et nom obligatoires": {"en": "SKU and name are required"},
    "Capacité bidon invalide": {"en": "Invalid drum capacity"},
    "Coût unitaire invalide": {"en": "Invalid unit cost"},
    "Consigne bouteille invalide (nombre de gallons > 0)":
        {"en": "Invalid bottle deposit (number of gallons > 0)"},
    "Sélectionne un produit à archiver": {"en": "Select a product to archive"},
    "Coche « Afficher les archivés », puis sélectionne le produit à réactiver":
        {"en": "Tick “Show archived”, then select the product to reactivate"},
    "« {sku} » n'est pas archivé : rien à réactiver":
        {"en": "“{sku}” is not archived: nothing to reactivate"},
    "Archiver « {sku} — {nom} » ?\n\nLe produit ne sera plus visible dans les "
    "listes et formulaires.\nCette action est réversible en base de données.":
        {"en": "Archive “{sku} — {nom}”?\n\nThe product will no longer appear "
               "in lists and forms.\nThis action can be reversed in the "
               "database."},
    "Réactiver « {sku} — {nom} » ?\n\nLe produit réapparaîtra dans les listes "
    "et les formulaires de bons.":
        {"en": "Reactivate “{sku} — {nom}”?\n\nThe product will reappear in "
               "lists and document forms."},
    "Produit « {sku} » archivé": {"en": "Product “{sku}” archived"},
    "Produit « {sku} » réactivé": {"en": "Product “{sku}” reactivated"},
    "Modifier — {sku}": {"en": "Edit — {sku}"},
    "Modifier {sku}": {"en": "Edit {sku}"},
    "{crees} produit(s) créé(s), {maj} mis à jour.":
        {"en": "{crees} product(s) created, {maj} updated."},
    "{lues} ligne(s) lue(s) → {refs} référence(s) unique(s).":
        {"en": "{lues} row(s) read → {refs} unique SKU(s)."},
    "⚠ {n} écart(s) de stock constaté(s), NON appliqué(s) :":
        {"en": "⚠ {n} stock discrepancy(ies) found, NOT applied:"},
    "   {sku} : système {systeme:g}, fichier {fichier:g}":
        {"en": "   {sku}: system {systeme:g}, file {fichier:g}"},
    "   … et {n} autre(s)": {"en": "   … and {n} more"},
    "   Corrige-les par un bon d'inventaire si le fichier fait foi.":
        {"en": "   Correct them with an inventory document if the file is "
               "authoritative."},
    "Signalements :": {"en": "Notices:"},
    "⚠ {n} ligne(s) rejetée(s) :": {"en": "⚠ {n} row(s) rejected:"},

    # ---------------------------------------------------------- utilisateurs
    "Nouvel utilisateur": {"en": "New user"},
    "Nom d'utilisateur": {"en": "Username"},
    "Nom complet": {"en": "Full name"},
    "Mot de passe": {"en": "Password"},
    "Actif": {"en": "Active"},
    "Inactif": {"en": "Inactive"},
    "Désactiver": {"en": "Deactivate"},
    "Activer": {"en": "Activate"},
    "Réinitialiser le mot de passe": {"en": "Reset password"},
    "👥  Gestion des utilisateurs": {"en": "👥  User management"},
    "Créer un utilisateur": {"en": "Create a user"},
    "Créer": {"en": "Create"},
    "Identifiant": {"en": "Username"},
    "Utilisateur": {"en": "User"},
    "Créé le": {"en": "Created on"},
    "Échec": {"en": "Failed"},
    "Magasinier = réception/expédition/inventaire  |  "
    "Lecteur = consultation seulement  |  "
    "Admin = tout  |  "
    "Entrepôt régional = réception, inventaire et alertes de SA région "
    "uniquement":
        {"en": "Warehouse operator = receiving/shipping/inventory  |  "
               "Read-only = viewing only  |  "
               "Admin = everything  |  "
               "Regional warehouse = receiving, inventory and alerts for "
               "THEIR region only"},
    "Changer le rôle": {"en": "Change role"},
    "Réinitialiser mot de passe": {"en": "Reset password"},
    "📋 Connexions": {"en": "📋 Sign-ins"},
    "{n} utilisateur(s)": {"en": "{n} user(s)"},
    "Sélectionne un utilisateur": {"en": "Select a user"},
    "Tous les champs requis (mot de passe 10+ caractères)":
        {"en": "All fields are required (password 10+ characters)"},
    "Choisis la région de ce compte régional":
        {"en": "Choose the region for this regional account"},
    "Choisis la région de ce compte":
        {"en": "Choose the region for this account"},
    "Utilisateur {nom} créé": {"en": "User {nom} created"},
    "Confirmation": {"en": "Confirmation"},
    "Réactiver {nom} ?": {"en": "Reactivate {nom}?"},
    "Désactiver {nom} ?": {"en": "Deactivate {nom}?"},
    "Chargement de l'historique…": {"en": "Loading history…"},
    "Historique de connexion": {"en": "Sign-in history"},
    "Dernières connexions": {"en": "Recent sign-ins"},
    "Rôle — {nom}": {"en": "Role — {nom}"},
    "Changer le rôle de {nom}": {"en": "Change the role of {nom}"},
    "Magasinier — réception, expédition, inventaire":
        {"en": "Warehouse operator — receiving, shipping, inventory"},
    "Lecteur — consultation seulement": {"en": "Read-only — viewing only"},
    "Administrateur — tout": {"en": "Administrator — everything"},
    "Entrepôt régional — sa région uniquement":
        {"en": "Regional warehouse — its own region only"},
    "Mot de passe — {nom}": {"en": "Password — {nom}"},
    "Nouveau mot de passe pour {nom}": {"en": "New password for {nom}"},
    "Nouveau mot de passe (10+ car.)": {"en": "New password (10+ chars)"},
    "Mot de passe changé pour {nom}": {"en": "Password changed for {nom}"},

    # ----------------------------------------------------------------- seuils
    "🔒  Seuils de réapprovisionnement": {"en": "🔒  Restocking thresholds"},
    "📅  Date début global": {"en": "📅  Global start date"},
    "Définis le stock minimum pour chaque produit. Le tableau de bord "
    "utilisera ces seuils pour les alertes.":
        {"en": "Set the minimum stock for each product. The dashboard uses "
               "these thresholds for its alerts."},
    "Seuil actuel": {"en": "Current threshold"},
    "Nouveau seuil": {"en": "New threshold"},
    "Enregistrer les seuils": {"en": "Save thresholds"},
    "Enregistrement...": {"en": "Saving..."},
    "Enregistrement en cours...": {"en": "Saving..."},
    "Aucune modification détectée": {"en": "No change detected"},
    "Aucune modification": {"en": "No change"},
    "{n} produit(s) — {en_attente} seuil(s) modifié(s) non enregistré(s)":
        {"en": "{n} product(s) — {en_attente} changed threshold(s) not saved"},
    "Seuil invalide pour « {sku} » : saisis un nombre (exemple : 12 ou 12.5)":
        {"en": "Invalid threshold for “{sku}”: enter a number "
               "(e.g. 12 or 12.5)"},
    "Le seuil de « {sku} » ne peut pas être négatif":
        {"en": "The threshold for “{sku}” cannot be negative"},
    "{n} seuil(s) mis à jour avec succès":
        {"en": "{n} threshold(s) updated successfully"},
    "Date stock début — Tous les produits":
        {"en": "Opening stock date — All products"},
    "Nouvelle période de stock": {"en": "New stock period"},
    "Applique la date de début à tous les produits":
        {"en": "Applies the start date to every product"},
    "Date début :": {"en": "Start date:"},
    "Aujourd'hui": {"en": "Today"},
    "Réinitialiser stock début = stock actuel pour tous":
        {"en": "Reset opening stock = current stock for all"},
    "Coche cette option pour démarrer une nouvelle période comptable":
        {"en": "Tick this option to start a new accounting period"},
    "Appliquer à tous": {"en": "Apply to all"},
    "Date invalide": {"en": "Invalid date"},
    "La date ne peut pas dépasser aujourd'hui":
        {"en": "The date cannot be later than today"},
    "Vérification du catalogue…": {"en": "Checking the catalogue…"},
    "TOUS les produits": {"en": "ALL products"},
    "Confirmer la réinitialisation": {"en": "Confirm reset"},
    "Le stock de début de {detail} va être remplacé par le stock actuel, "
    "avec la date du {date}.\n\nCette action est IRRÉVERSIBLE : les anciens "
    "stocks de début seront définitivement perdus.\n\nContinuer ?":
        {"en": "The opening stock of {detail} will be replaced by the current "
               "stock, dated {date}.\n\nThis action is IRREVERSIBLE: the "
               "previous opening stocks will be lost for good.\n\nContinue?"},
    "Réinitialisation annulée": {"en": "Reset cancelled"},
    "Date début mise à jour pour {n} produit(s).":
        {"en": "Start date updated for {n} product(s)."},
    "Stock début réinitialisé au stock actuel.":
        {"en": "Opening stock reset to the current stock."},

    # --------------------------------------------------------------- contacts
    "📇  Contacts (Fournisseurs / Clients)":
        {"en": "📇  Contacts (Suppliers / Customers)"},
    "Ajouter un contact": {"en": "Add a contact"},
    "Modifier le contact": {"en": "Edit contact"},
    "Modifier — {nom}": {"en": "Edit — {nom}"},
    "Autre": {"en": "Other"},
    "Téléphone": {"en": "Phone"},
    "Téléphone :": {"en": "Phone:"},
    "Email": {"en": "Email"},
    "Email :": {"en": "Email:"},
    "Adresse": {"en": "Address"},
    "Adresse :": {"en": "Address:"},
    "Type :": {"en": "Type:"},
    "Le nom est obligatoire": {"en": "The name is required"},
    "Contact « {nom} » existe déjà": {"en": "Contact “{nom}” already exists"},
    "Sélectionne un contact": {"en": "Select a contact"},
    "Désactiver / Réactiver": {"en": "Deactivate / Reactivate"},
    "Réactiver « {nom} » ?": {"en": "Reactivate “{nom}”?"},
    "Désactiver « {nom} » ?": {"en": "Deactivate “{nom}”?"},
    "{shown} / {total} contact(s)": {"en": "{shown} / {total} contact(s)"},
    "{n} contact(s)": {"en": "{n} contact(s)"},
    "Lecture seule : un lecteur ne peut pas créer de contacts":
        {"en": "Read-only: a read-only account cannot create contacts"},
    "Lecture seule : un lecteur ne peut pas modifier de contacts":
        {"en": "Read-only: a read-only account cannot edit contacts"},

    # -------------------------------------------------------- journal d'audit
    "📋  Journal d'audit": {"en": "📋  Audit log"},
    "Action": {"en": "Action"},
    "Entité": {"en": "Entity"},
    "Tout": {"en": "All"},
    "Bons": {"en": "Documents"},
    "Détail de la modification": {"en": "Change details"},
    "{n} entrée(s)": {"en": "{n} entry(ies)"},
    "{affiche} entrée(s) sur {total}": {"en": "{affiche} of {total} entries"},
    "Date     : {valeur}": {"en": "Date     : {valeur}"},
    "Utilisateur : {valeur}": {"en": "User        : {valeur}"},
    "Action   : {valeur}": {"en": "Action   : {valeur}"},
    "Entité   : {type} #{id}": {"en": "Entity   : {type} #{id}"},
    "Avant :": {"en": "Before:"},
    "Avant : {valeur}": {"en": "Before: {valeur}"},
    "Après :": {"en": "After:"},
    "Après : {valeur}": {"en": "After: {valeur}"},

    # ------------------------------------------------------------- bouteilles
    "🍾  Consignes bouteilles": {"en": "🍾  Bottle deposits"},
    "Suivi des bouteilles vides dues par région pour les produits consignés.":
        {"en": "Tracking of empty bottles owed by each region for products "
               "with a deposit."},
    "Regrouper par :": {"en": "Group by:"},
    "Technicien": {"en": "Technician"},
    "Bouteilles dues": {"en": "Bottles owed"},
    "N° de fiche": {"en": "Return slip no."},
    "N° de fiche (facultatif)": {"en": "Slip no. (optional)"},
    "Par": {"en": "By"},
    "Nombre de bouteilles": {"en": "Number of bottles"},
    "Enregistrer un retour de bouteilles": {"en": "Record a bottle return"},
    "Bouteilles vides rapportées physiquement par la région. Le stock produit "
    "n'est pas modifié.":
        {"en": "Empty bottles physically brought back by the region. Product "
               "stock is not affected."},
    # « ✓  Enregistrer le retour » est déjà défini plus haut (TYPE_LABELS).
    "Lecture seule : un lecteur ne peut pas enregistrer de retour":
        {"en": "Read-only: a read-only account cannot record a return"},
    "Derniers retours de bouteilles": {"en": "Recent bottle returns"},
    "Annuler le retour sélectionné": {"en": "Cancel the selected return"},
    "Sélectionne une ligne à annuler.": {"en": "Select a row to cancel."},
    "Confirmer l'annulation": {"en": "Confirm cancellation"},
    "Annuler le retour #{id} ?\n\n"
    "Région : {region}\n"
    "Technicien : {technicien}\n"
    "Produit : {produit}\n"
    "Bouteilles : {bouteilles}\n\n"
    "Les bouteilles seront à nouveau comptées comme dues.":
        {"en": "Cancel return #{id}?\n\n"
               "Region: {region}\n"
               "Technician: {technicien}\n"
               "Product: {produit}\n"
               "Bottles: {bouteilles}\n\n"
               "The bottles will be counted as owed again."},
    "Annulation…": {"en": "Cancelling…"},
    "Retour annulé": {"en": "Return cancelled"},
    "Retour #{id} annulé.\nNouveau solde : {solde:g} bouteille(s) dues.":
        {"en": "Return #{id} cancelled.\nNew balance: {solde:g} bottle(s) "
               "owed."},
    "{sku} — {nom} ({gls:g} gls/bouteille)":
        {"en": "{sku} — {nom} ({gls:g} gls/bottle)"},
    "Aucune bouteille due par un technicien. Renseigne le champ Technicien "
    "sur une expédition pour activer ce suivi.":
        {"en": "No bottles owed by any technician. Fill in the Technician "
               "field on a shipment to enable this tracking."},
    "Aucune bouteille due. Configure la consigne d'un produit dans Produits "
    "(double-clic) pour activer le suivi.":
        {"en": "No bottles owed. Set a product's deposit in Products "
               "(double-click) to enable tracking."},
    "{n} ligne(s) — {total:g} bouteille(s) due(s) au total":
        {"en": "{n} row(s) — {total:g} bottle(s) owed in total"},
    "Choisis un produit consigné": {"en": "Choose a product with a deposit"},
    "Choisis une région": {"en": "Choose a region"},
    "Nombre de bouteilles invalide": {"en": "Invalid number of bottles"},
    "Le nombre de bouteilles doit être supérieur à zéro":
        {"en": "The number of bottles must be greater than zero"},
    "Enregistrement en cours…": {"en": "Saving…"},
    "Retour enregistré": {"en": "Return saved"},
    "{n:g} bouteille(s) retournée(s) par {par}.":
        {"en": "{n:g} bottle(s) returned by {par}."},
    "Fiche n° {ref}.": {"en": "Slip no. {ref}."},
    "Solde restant pour la région {region} : {solde:g} bouteille(s).":
        {"en": "Remaining balance for region {region}: {solde:g} bottle(s)."},
    "Solde personnel de {technicien} : {solde:g} bouteille(s).":
        {"en": "Personal balance for {technicien}: {solde:g} bottle(s)."},
    "Le serveur ne répond pas.\n\nVérifie la connexion réseau et réessaie.":
        {"en": "The server is not responding.\n\nCheck the network connection "
               "and try again."},
    "Retour de bouteilles impossible": {"en": "Bottle return failed"},

    # --------------------------------------------------- entrepôts régionaux
    "🏬  Entrepôts régionaux — Consommables":
        {"en": "🏬  Regional warehouses — Consumables"},
    "Région :": {"en": "Region:"},
    "Article": {"en": "Item", "ht": "Atik"},
    "Tous les articles": {"en": "All items", "ht": "Tout atik yo"},
    "Rechercher un article…": {"en": "Search for an item…", "ht": "Chèche yon atik…"},
    "Commentaire (facultatif)": {"en": "Comment (optional)", "ht": "Kòmantè (opsyonèl)"},

    # ---------------------------------------------------- réception régionale
    "📥  Réception régionale": {"en": "📥  Regional receiving", "ht": "📥  Resepsyon rejyonal"},
    "📥  Réception — {region}": {"en": "📥  Receiving — {region}", "ht": "📥  Resepsyon — {region}"},
    "📥  Transferts en attente de réception":
        {"en": "📥  Transfers awaiting receipt"},
    "📥  Transferts en attente — {region}":
        {"en": "📥  Pending transfers — {region}"},
    "Bons partis de l'entrepôt central, en attente que la région confirme ce "
    "qu'elle a reçu. Lecture seule : c'est à la région de confirmer, pas au "
    "central à sa place.":
        {"en": "Documents that left the central warehouse, waiting for the "
               "region to confirm what it received. Read-only: it is up to "
               "the region to confirm, not the head office on its behalf."},
    "Les bons ci-dessous ont quitté l'entrepôt central et sont en route. "
    "Sélectionne un bon, puis clique sur « Confirmer la réception » pour "
    "saisir les quantités réellement reçues.":
        {"en": "The documents below have left the central warehouse and are "
               "in transit. Select one, then click “Confirm receipt” to enter "
               "the quantities actually received.", "ht": "Bon ki anba yo kite depo santral la, y ap vini. Chwazi yon bon, epi klike sou « Konfime resepsyon an » pou w antre kantite ou resevwa vre a."},
    "Expédié le": {"en": "Shipped on", "ht": "Voye le"},
    "Contenu": {"en": "Contents", "ht": "Sa ki ladan"},
    "Quantité totale": {"en": "Total quantity", "ht": "Kantite total"},
    "Expédié par": {"en": "Shipped by", "ht": "Voye pa"},
    "✓  Confirmer la réception": {"en": "✓  Confirm receipt", "ht": "✓  Konfime resepsyon an"},
    # « Confirmer la réception » est déjà défini plus haut (document_form).
    "{n} transfert(s) en attente de confirmation":
        {"en": "{n} transfer(s) awaiting confirmation", "ht": "{n} transfè k ap tann konfimasyon"},
    "Aucun transfert en attente": {"en": "No pending transfer", "ht": "Pa gen transfè k ap tann"},
    # Relance des transferts qui traînent (écrans Réception régionale et
    # Transferts entre régions). Traduits en créole : ce sont des écrans de
    # compte régional, et c'est le magasinier qui doit décider de relancer.
    "En attente": {"en": "Waiting", "ht": "K ap tann"},
    "{jours} j": {"en": "{jours} d", "ht": "{jours} j"},
    "{n} transfert(s) en attente, dont {r} depuis plus de {j} jour(s) "
    "(lignes en rouge) : rappelle l'expéditeur ou confirme ce que tu as reçu.":
        {"en": "{n} transfer(s) waiting, {r} of them for more than {j} day(s) "
               "(rows in red): call the sender back, or confirm what you "
               "received.",
         "ht": "{n} transfè k ap tann, {r} ladan yo depi plis pase {j} jou "
               "(liy wouj yo) : rele moun ki voye a, oswa konfime sa ou "
               "resevwa."},
    "{n} envoi(s) en attente, dont {r} depuis plus de {j} jour(s) (lignes en "
    "rouge) : appelle la région destinataire pour qu'elle confirme.":
        {"en": "{n} shipment(s) waiting, {r} of them for more than {j} day(s) "
               "(rows in red): call the receiving region so it confirms.",
         "ht": "{n} voye k ap tann, {r} ladan yo depi plis pase {j} jou (liy "
               "wouj yo) : rele rejyon k ap resevwa a pou l konfime."},
    "+{n} autre(s)": {"en": "+{n} more"},
    "Sélectionne d'abord un transfert": {"en": "First select a transfer", "ht": "Chwazi yon transfè anvan"},
    "Réception — {ref}": {"en": "Receipt — {ref}", "ht": "Resepsyon — {ref}"},
    "Quantités réellement reçues": {"en": "Quantities actually received", "ht": "Kantite ou resevwa vre"},
    "Corrige la quantité si tout n'est pas arrivé. Un écart est simplement "
    "signalé : il ne bloque rien.":
        {"en": "Correct the quantity if not everything arrived. A "
               "discrepancy is only flagged; it never blocks anything.", "ht": "Korije kantite a si tout bagay pa rive. Yon diferans jis siyale, li pa bloke anyen."},
    "Expédié": {"en": "Shipped", "ht": "Voye"},
    # « Reçu » est déjà défini plus haut (statuts de l'historique).
    "Ex. : un carton ouvert à l'arrivée":
        {"en": "e.g. one box opened on arrival", "ht": "Egz. : yon katon te louvri lè l rive"},
    "conforme": {"en": "matches", "ht": "kòrèk"},
    "{ecart:+g} — impossible": {"en": "{ecart:+g} — not possible"},
    "Quantité invalide pour « {nom} » (nombre positif ou zéro attendu)":
        {"en": "Invalid quantity for “{nom}” (a positive number or zero is "
               "expected)"},
    "« {nom} » : {recu:g} reçus alors que {envoye:g} seulement ont été "
    "expédiés":
        {"en": "“{nom}”: {recu:g} received while only {envoye:g} were "
               "shipped"},
    "Écart constaté": {"en": "Discrepancy found", "ht": "Gen yon diferans"},
    "{n} article(s) reçus en quantité différente de celle expédiée.\n\n"
    "Seules les quantités reçues seront ajoutées au stock de la région.\n\n"
    "Confirmer quand même ?":
        {"en": "{n} item(s) received in a quantity different from the one "
               "shipped.\n\nOnly the received quantities will be added to the "
               "region's stock.\n\nConfirm anyway?"},
    "Réception confirmée": {"en": "Receipt confirmed", "ht": "Resepsyon konfime"},
    "Réception enregistrée avec {n} écart(s).\nL'entrepôt central en est "
    "informé.":
        {"en": "Receipt saved with {n} discrepancy(ies).\nThe central "
               "warehouse has been notified."},
    "Réception enregistrée, tout est conforme.":
        {"en": "Receipt saved, everything matches.", "ht": "Resepsyon anrejistre, tout bagay kòrèk."},

    # --------------------------------------------------- inventaire régional
    "📋  Inventaire régional": {"en": "📋  Regional inventory", "ht": "📋  Envantè rejyonal"},
    "📋  Inventaire — {region}": {"en": "📋  Inventory — {region}", "ht": "📋  Envantè — {region}"},
    "📋  Comptages reçus des régions":
        {"en": "📋  Counts received from the regions"},
    "📋  Comptages reçus — {region}": {"en": "📋  Counts received — {region}"},
    "🕐  Comptages précédents": {"en": "🕐  Previous counts", "ht": "🕐  Konte anvan yo"},
    "Comptages précédents": {"en": "Previous counts", "ht": "Konte anvan yo"},
    "Comptages déjà envoyés": {"en": "Counts already sent", "ht": "Konte ki deja voye"},
    "Comptages envoyés par la région — aucune saisie ici, c'est la région qui "
    "compte son propre stock.":
        {"en": "Counts sent by the region — no data entry here; the region "
               "counts its own stock."},
    "Saisis la quantité comptée pour chaque article. Ce comptage est "
    "informatif : il ne modifie aucun stock, il informe l'entrepôt central de "
    "ce qu'il te reste.":
        {"en": "Enter the counted quantity for each item. This count is "
               "informational: it changes no stock, it tells the central "
               "warehouse what you have left.", "ht": "Antre kantite ou konte pou chak atik. Kontaj sa a se pou enfòmasyon : li pa chanje okenn stòk, li fè depo santral la konnen sa ki rete lakay ou."},
    "Date du comptage": {"en": "Count date"},
    "Quantité comptée": {"en": "Counted quantity"},
    "Saisi par": {"en": "Entered by", "ht": "Antre pa"},
    "Ex. : comptage de fin de mois": {"en": "e.g. end-of-month count", "ht": "Egz. : kontaj fen mwa"},
    "📤  Envoyer le comptage": {"en": "📤  Send the count", "ht": "📤  Voye kontaj la"},
    "{n} ligne(s) comptée(s) — le comptage le plus récent est en haut":
        {"en": "{n} counted row(s) — the most recent count is at the top"},
    "Aucun comptage envoyé pour l'instant.": {"en": "No count sent yet."},
    "Aucun produit dans le catalogue.": {"en": "No product in the catalogue.", "ht": "Pa gen pwodwi nan katalòg la."},
    "Rien à compter": {"en": "Nothing to count", "ht": "Pa gen anyen pou konte"},
    "Aucun article ne correspond à ta recherche.":
        {"en": "No item matches your search.", "ht": "Pa gen atik ki koresponn ak rechèch ou a."},
    "{n} article(s) au catalogue": {"en": "{n} item(s) in the catalogue", "ht": "{n} atik nan katalòg la"},
    "Quantité invalide pour « {nom} »": {"en": "Invalid quantity for “{nom}”"},
    "Quantité négative pour « {nom} »": {"en": "Negative quantity for “{nom}”"},
    "Saisis au moins une quantité comptée":
        {"en": "Enter at least one counted quantity", "ht": "Antre omwen yon kantite ou konte"},
    "Comptage envoyé": {"en": "Count sent", "ht": "Kontaj voye"},
    "{n} ligne(s) transmises à l'entrepôt central.\n\nAucun stock n'a été "
    "modifié : ce comptage est informatif.":
        {"en": "{n} row(s) sent to the central warehouse.\n\nNo stock was "
               "changed: this count is informational."},

    # ---------------------------------------------------- alertes régionales
    "⚠  Alertes régionales": {"en": "⚠  Regional alerts", "ht": "⚠  Alèt rejyonal"},
    "⚠  Alertes régionales — toutes régions":
        {"en": "⚠  Regional alerts — all regions"},
    "⚠  Alertes — {region}": {"en": "⚠  Alerts — {region}", "ht": "⚠  Alèt — {region}"},
    "Ruptures signalées par les régions, toutes confondues, pas encore "
    "traitées. Marque un signalement résolu une fois le réapprovisionnement "
    "expédié.":
        {"en": "Stock-outs reported by the regions, all regions combined, not "
               "yet handled. Mark a report resolved once the restock has "
               "shipped."},
    "Code": {"en": "Code"},
    "Précision": {"en": "Detail"},
    "Signalé par": {"en": "Reported by"},
    "Le": {"en": "On"},
    "✓  Marquer résolu": {"en": "✓  Mark resolved"},
    "Marquer résolu": {"en": "Mark resolved"},
    "Signale à l'entrepôt central un article dont tu manques, sans attendre "
    "l'inventaire. Un clic sur « Signaler » suffit ; la précision est "
    "facultative.":
        {"en": "Report to the central warehouse an item you are out of, "
               "without waiting for the inventory. One click on “Report” is "
               "enough; the detail is optional.", "ht": "Fè depo santral la konnen yon atik ou pa genyen ankò, san w pa tann envantè a. Yon klik sou « Siyale » sifi ; presizyon an opsyonèl."},
    "Déjà signalé, en attente du central":
        {"en": "Already reported, awaiting head office", "ht": "Deja siyale, k ap tann santral la"},
    "⚠  {n} rupture(s) signalée(s) et non traitée(s)":
        {"en": "⚠  {n} stock-out(s) reported and not yet handled"},
    "✓  Aucune rupture signalée": {"en": "✓  No stock-out reported"},
    "Sélectionne d'abord un signalement": {"en": "First select a report"},
    "Confirmer que « {nom} » a été réapprovisionné pour {region} ?\n\n"
    "Le signalement disparaîtra de la liste.":
        {"en": "Confirm that “{nom}” has been restocked for {region}?\n\n"
               "The report will disappear from the list."},
    "{n} signalement(s) en attente du central":
        {"en": "{n} report(s) awaiting head office", "ht": "{n} siyalman k ap tann santral la"},
    "Aucun signalement en cours": {"en": "No report in progress", "ht": "Pa gen siyalman kounye a"},
    "Rien de signalé pour l'instant.": {"en": "Nothing reported yet.", "ht": "Pa gen anyen ki siyale pou kounye a."},
    "Aucun article dans le catalogue.": {"en": "No item in the catalogue.", "ht": "Pa gen atik nan katalòg la."},
    "déjà signalé": {"en": "already reported", "ht": "deja siyale"},
    "⚠  Signaler": {"en": "⚠  Report", "ht": "⚠  Siyale"},
    "Envoi du signalement…": {"en": "Sending the report…"},
    "Signalé": {"en": "Reported", "ht": "Siyale"},
    "« {nom} » signalé à l'entrepôt central.":
        {"en": "“{nom}” reported to the central warehouse.", "ht": "« {nom} » siyale bay depo santral la."},
    "Signalement impossible": {"en": "Report failed", "ht": "Siyalman pa posib"},
    "Signaler une rupture": {"en": "Report a stock-out", "ht": "Siyale yon mank"},
    "Signaler à l'entrepôt central": {"en": "Report to the central warehouse", "ht": "Siyale bay depo santral la"},
    "Précision (facultative)": {"en": "Detail (optional)", "ht": "Presizyon (opsyonèl)"},
    "Ex. : plus rien depuis lundi": {"en": "e.g. nothing left since Monday", "ht": "Egz. : nou pa gen anyen depi lendi"},

    # ---------------------------------------------------------- impression PDF
    "Impossible d'enregistrer le PDF :\n{e}":
        {"en": "Could not save the PDF:\n{e}"},

    # -------------------------------------------------------------- dialogues
    "Ne plus demander": {"en": "Don't ask again"},

    # ------------------------------------------ santé du référentiel produits
    "Qualité référentiel": {"en": "Catalogue health"},
    "🩺  Santé du référentiel produits":
        {"en": "🩺  Product catalogue health"},
    "Tous les défauts": {"en": "All issues"},
    "Défauts relevés": {"en": "Issues found"},
    "Désignation": {"en": "Description"},
    "Aucune correction automatique : double-cliquez une ligne pour ouvrir la "
    "fiche produit et corriger.":
        {"en": "Nothing is fixed automatically: double-click a row to open the "
               "product sheet and correct it."},
    "Aucun défaut relevé sur les {total} produits actifs.":
        {"en": "No issue found across the {total} active products."},
    "{affiche} fiche(s) affichée(s) — {n} en défaut sur {total} produits actifs.":
        {"en": "{affiche} record(s) shown — {n} with issues out of {total} "
               "active products."},
    " (idem {skus})": {"en": " (same as {skus})"},
    # Libellés de défaut : ils correspondent aux codes renvoyés par
    # server/qualite_referentiel.py, retraduits côté client.
    "Catégorie non renseignée": {"en": "Category missing"},
    "Coût unitaire non renseigné": {"en": "Unit cost missing"},
    "Produit en volume sans capacité de bidon":
        {"en": "Volume product without drum capacity"},
    "Désignation très proche d'un autre produit actif":
        {"en": "Description almost identical to another active product"},
    "Désignation vide": {"en": "Description empty"},
    "Désignation identique au code SKU": {"en": "Description same as the SKU"},
    "Catégorie :": {"en": "Category:"},
    "vide = non classé": {"en": "empty = unclassified"},

    # ------------------------------------------------ périmètre des lecteurs
    "🌍 Régions du lecteur": {"en": "🌍 Reader's regions"},
    "Régions — {nom}": {"en": "Regions — {nom}"},
    "Régions visibles par ce lecteur": {"en": "Regions this reader can see"},
    "Aucune case cochée = ce lecteur voit TOUTES les régions.":
        {"en": "No box ticked = this reader sees ALL regions."},
    "Comptes lecteurs uniquement": {"en": "Reader accounts only"},
    "Seul un compte « Lecteur » peut être restreint à des régions.\n\n"
    "Un compte régional est déjà limité à sa région, un magasinier travaille "
    "pour l'entrepôt central et un administrateur doit tout voir.":
        {"en": "Only a « Reader » account can be restricted to "
               "regions.\n\nA regional account is already limited to its own "
               "region, a storekeeper works for the central warehouse, and an "
               "administrator must see everything."},

    # ------------------------------------------------------ revue des comptes
    "🔎 Revue des comptes": {"en": "🔎 Account review"},
    "Revue des comptes": {"en": "Account review"},
    "Chargement de la revue…": {"en": "Loading the review…"},
    "Aucune désactivation automatique : cette liste sert à décider, pas à agir.":
        {"en": "Nothing is disabled automatically: this list is for deciding, "
               "not for acting."},
    "Dernière connexion": {"en": "Last sign-in"},
    "Inactif depuis": {"en": "Inactive for"},
    "Régions autorisées": {"en": "Allowed regions"},
    "Jamais": {"en": "Never"},
    "créé il y a {n} j": {"en": "created {n} d ago"},
    "{n} j": {"en": "{n} d"},

    # --------------------------------------------------- notes par région
    "📝  Note de région (saisonnalité, accès) :":
        {"en": "📝  Region note (seasonality, access):"},
    "Ex. : route difficile juin-septembre":
        {"en": "e.g. road difficult June–September"},
    "Enregistré": {"en": "Saved"},

    # ------------------------------------------------------------------ photos
    # Écrans du central (fiche produit, historique) : pas de créole, le repli
    # sur le français est le comportement voulu (voir l'en-tête du module).
    "📷  Changer la photo": {"en": "📷  Change photo"},
    "📷  Photos": {"en": "📷  Photos"},
    "📷  Ajouter une photo": {"en": "📷  Add a photo"},
    "Retirer la photo": {"en": "Remove photo"},
    "Retirer la photo de « {sku} » ?": {"en": "Remove the photo of “{sku}”?"},
    "Aucune photo": {"en": "No photo"},
    "Aucune photo pour ce bon.": {"en": "No photo for this document."},
    "Photo illisible": {"en": "Unreadable photo"},
    "Photo indisponible": {"en": "Photo unavailable"},
    "Photo refusée": {"en": "Photo rejected"},
    "Photo du produit": {"en": "Product photo"},
    "Photo à joindre au bon": {"en": "Photo to attach to the document"},
    "Photos du bon n°{id}": {"en": "Photos of document #{id}"},
    "Pièces jointes": {"en": "Attachments"},
    "Photos rattachées à ce bon : colis abîmé, bordereau signé, état de la "
    "marchandise à l'arrivée.":
        {"en": "Photos attached to this document: damaged parcel, signed "
               "delivery note, condition of the goods on arrival."},
    "Ajoutée par {qui} le {quand}": {"en": "Added by {qui} on {quand}"},
    "{n} photo(s)": {"en": "{n} photo(s)"},
    "Légende de la photo": {"en": "Photo caption"},
    "Légende (facultative)": {"en": "Caption (optional)"},
    "Ex. : carton éventré côté droit": {"en": "e.g. box torn open on the right"},
    "Envoyer": {"en": "Send"},
    "Affichage des photos indisponible sur ce poste":
        {"en": "Photo display unavailable on this workstation"},

    # ------------------------------------------------ indicateurs de pilotage
    # Écran réservé à l'administration centrale : FR/EN seulement, pas de
    # créole (aucun compte régional n'y a accès, voir l'en-tête de ce module).
    "Indicateurs": {"en": "Metrics"},
    "📈  Indicateurs de pilotage": {"en": "📈  Management metrics"},
    "Lecture seule. Onze mesures de santé de l'entrepôt central et du "
    "réseau régional.":
        {"en": "Read only. Eleven health measures of the central warehouse and "
               "the regional network."},
    "Précision d'inventaire": {"en": "Inventory accuracy"},
    "Concentration régions": {"en": "Regional concentration"},
    "Comptages à temps": {"en": "Counts on time"},
    "Anomalies du mois": {"en": "Anomalies this month"},
    # 1. précision d'inventaire
    "Précision moyenne": {"en": "Average accuracy"},
    "Précision pondérée": {"en": "Weighted accuracy"},
    "Produits comptés": {"en": "Products counted"},
    "Comptages conformes": {"en": "Counts within target"},
    "Écart absolu cumulé": {"en": "Total absolute variance"},
    "Théorique": {"en": "Expected"},
    # « Compté », « Écart » et « Comptage » sont déjà traduits plus haut
    # (écran Inventaire) : les redéfinir ici écraserait silencieusement la
    # version créole. Idem « Précision », qui vaut « Detail » ailleurs — d'où
    # l'intitulé distinct « Précision (%) » pour la colonne de cet écran.
    "Précision (%)": {"en": "Accuracy (%)"},
    "Aucun comptage d'inventaire enregistré : la précision ne peut pas encore "
    "être mesurée.":
        {"en": "No stock count recorded yet: accuracy cannot be measured."},
    "Cycle du {depuis} au {dernier} ({jours} jour(s)). Précision d'une ligne = "
    "100 × (1 − |écart| / |théorique|), le théorique étant reconstitué comme "
    "compté − écart.":
        {"en": "Cycle from {depuis} to {dernier} ({jours} day(s)). Line "
               "accuracy = 100 × (1 − |variance| / |expected|), the expected "
               "figure being rebuilt as counted − variance."},
    # 2. concentration des régions
    "Volume livré (90 j)": {"en": "Volume shipped (90 d)"},
    "Régions servies": {"en": "Regions served"},
    "Régions faisant 80 %": {"en": "Regions making 80%"},
    "Rang": {"en": "Rank"},
    "Part": {"en": "Share"},
    "Cumul": {"en": "Cumulative"},
    "Aucune expédition sur la période.":
        {"en": "No shipment over the period."},
    "Sur {jours} jours, {n} région(s) concentrent 80 % du volume livré "
    "(lignes en orange).":
        {"en": "Over {jours} days, {n} region(s) account for 80% of the "
               "volume shipped (rows in orange)."},
    # 3. comptages régionaux rendus à temps
    "Taux du mois": {"en": "Rate this month"},
    "Comptages rendus": {"en": "Counts received"},
    "Régions en retard": {"en": "Regions overdue"},
    "Lignes comptées": {"en": "Lines counted"},
    "Jours de comptage": {"en": "Counting days"},
    "Dernier comptage": {"en": "Last count"},
    "Rendu": {"en": "Received"},
    "Non rendu": {"en": "Missing"},
    "Mois de {mois} : {rendus} région(s) sur {total} à entrepôt régional ont "
    "rendu un comptage.":
        {"en": "Month {mois}: {rendus} of {total} regions with a warehouse "
               "have submitted a count."},
    " En retard : {liste}.": {"en": " Overdue: {liste}."},
    " Comptages hors référentiel : {liste}.":
        {"en": " Counts outside the reference list: {liste}."},
    # 4. anomalies de mouvement
    "Anomalies détectées": {"en": "Anomalies detected"},
    "Écart maximal": {"en": "Largest deviation"},
    "Moyenne habituelle": {"en": "Usual average"},
    "Aucun mouvement inhabituel ce mois-ci : toutes les quantités restent "
    "proches des habitudes de leur produit.":
        {"en": "No unusual movement this month: every quantity stays close to "
               "its product's usual pattern."},
    "Mouvements du mois dont la quantité dépasse {facteur}× la moyenne du "
    "même produit sur les mois précédents ({mini} mouvements historiques "
    "minimum). Un écart n'est pas une faute : il se vérifie.":
        {"en": "This month's movements whose quantity exceeds {facteur}× the "
               "same product's average over previous months (at least {mini} "
               "historical movements). A deviation is not a fault: check it."},

    # --- Indicateurs 6 à 9 : dormants, alertes, consigne, opérateurs ---
    # Seules les clés PROPRES à ces sections sont posées ici. « Région »,
    # « Stock », « Catégorie », « Désignation », « Part » ou « Technicien »
    # existent déjà plus haut : les redéfinir écraserait silencieusement leur
    # version créole (voir test_aucune_cle_i18n_dupliquee).
    "Produits dormants": {"en": "Dormant products"},
    "Résolution des alertes": {"en": "Alert resolution"},
    "Dette de consigne": {"en": "Bottle deposit debt"},
    "Activité opérateurs": {"en": "Operator activity"},
    # 6. produits dormants
    "Produits sans sortie": {"en": "Products with no outflow"},
    "Dont encore en stock": {"en": "Of which still in stock"},
    "Valeur immobilisée": {"en": "Tied-up value"},
    "Jamais sortis": {"en": "Never went out"},
    # « Coût unitaire » et « Bouteilles dues » sont déjà traduits plus haut
    # (écrans Produits et Consigne) : ne pas les redéfinir ici.
    "Jours sans sortie": {"en": "Days with no outflow"},
    "Dernière sortie": {"en": "Last outflow"},
    "non renseigné": {"en": "not set"},
    "jamais": {"en": "never"},
    "Aucun produit dormant : tout le catalogue actif a connu au moins une "
    "sortie sur la période.":
        {"en": "No dormant product: every active item went out at least once "
               "over the period."},
    "Produits actifs sans aucune sortie (expédition, transfert régional, ou "
    "ajustement à la baisse) depuis {jours} jours. La valeur est stock × coût "
    "unitaire.":
        {"en": "Active products with no outflow (delivery, regional transfer, "
               "or downward adjustment) for {jours} days. Value is stock × "
               "unit cost."},
    " {n} produit(s) en stock n'ont pas de coût unitaire : la valeur "
    "immobilisée est donc sous-estimée.":
        {"en": " {n} product(s) in stock have no unit cost: the tied-up value "
               "is therefore understated."},
    # 7. résolution des alertes
    "Taux de résolution": {"en": "Resolution rate"},
    "Signalements ouverts": {"en": "Open reports"},
    "Délai moyen": {"en": "Average delay"},
    "Signalements": {"en": "Reports"},
    "Résolus": {"en": "Resolved"},
    "Ouverts": {"en": "Open"},
    "Délai moyen (h)": {"en": "Average delay (h)"},
    "Délai max (h)": {"en": "Longest delay (h)"},
    "Plus ancien ouvert (j)": {"en": "Oldest still open (d)"},
    "{h} h": {"en": "{h} h"},
    "Aucun signalement de rupture régionale sur la période.":
        {"en": "No regional shortage reported over the period."},
    "Signalements ouverts par les régions sur {jours} jours. Le délai court "
    "de l'ouverture par la région à la résolution par le central. Une région "
    "à fort taux mais dont le plus ancien signalement traîne n'est pas bien "
    "servie.":
        {"en": "Shortages reported by regions over {jours} days. The delay "
               "runs from the region opening the report to the central "
               "warehouse resolving it. A region with a high rate whose "
               "oldest report drags on is not well served."},
    # 8. dette de consigne
    "Détenteurs en dette": {"en": "Holders in debt"},
    "Dette la plus ancienne": {"en": "Oldest debt"},
    "Mouvements": {"en": "Movements"},
    "Dernier mouvement": {"en": "Last movement"},
    "Ancienneté (j)": {"en": "Age (d)"},
    "(région seule)": {"en": "(region only)"},
    "{j} j": {"en": "{j} d"},
    "Aucune bouteille consignée en attente de retour.":
        {"en": "No deposit bottle awaiting return."},
    "Solde net par détenteur : bouteilles parties moins bouteilles rendues. "
    "Une livraison nominative apparaît sur la ligne du technicien, jamais en "
    "double avec sa région.":
        {"en": "Net balance per holder: bottles out minus bottles returned. A "
               "named delivery shows on the technician's row, never duplicated "
               "on their region's."},
    " {n} solde(s) négatif(s) : plus de retours que de sorties, à corriger.":
        {"en": " {n} negative balance(s): more returns than issues, to be "
               "corrected."},
    # 9. activité opérateurs
    "Opérateurs actifs": {"en": "Active operators"},
    "Bons saisis": {"en": "Documents entered"},
    "Saisies hors horaires": {"en": "Out-of-hours entries"},
    "Annulés": {"en": "Cancelled"},
    "Hors horaires": {"en": "Out of hours"},
    "Heure de pointe": {"en": "Peak hour"},
    "Dernière saisie": {"en": "Last entry"},
    "{n} ({p})": {"en": "{n} ({p})"},
    "Aucun bon saisi sur la période.":
        {"en": "No document entered over the period."},
    "Bons créés sur {jours} jours, par compte de saisie (annulés compris : le "
    "bon a quand même été tapé). « Hors horaires » = saisie avant {debut} h "
    "ou après {fin} h. Ce n'est pas une faute : c'est une question à poser.":
        {"en": "Documents created over {jours} days, by entering account "
               "(cancelled ones included: the document was still typed). "
               "« Out of hours » = entered before {debut} h or after "
               "{fin} h. Not a fault: a question worth asking."},
    # ---------------------------------------- 10. écarts de réception
    "Écarts de réception": {"en": "Reception discrepancies"},
    "Manquants (jamais arrivés)": {"en": "Short (never arrived)"},
    "Excédents (reçus en trop)": {"en": "Over (received in excess)"},
    "Taux de manquant": {"en": "Shortfall rate"},
    "Réceptions en écart": {"en": "Receptions with a discrepancy"},
    "Région destinataire": {"en": "Destination region"},
    "Manquants": {"en": "Short"},
    "Excédents": {"en": "Over"},
    "Net": {"en": "Net"},
    "Taux manquant": {"en": "Shortfall rate"},
    "(non renseigné)": {"en": "(not specified)"},
    "Aucune réception confirmée sur la période : rien à comparer.":
        {"en": "No confirmed reception over the period: nothing to compare."},
    "Écart entre la quantité expédiée et la quantité confirmée reçue, cumulé "
    "sur {jours} jours par région destinataire et par transporteur. Manquants "
    "et excédents sont comptés séparément à dessein : les additionner "
    "effacerait deux anomalies l'une par l'autre.":
        {"en": "Gap between the quantity shipped and the quantity confirmed "
               "received, totalled over {jours} days by destination region and "
               "by carrier. Short and over are counted separately on purpose: "
               "adding them up would cancel two anomalies against each other."},
    " (Le compte de bons en écart est plafonné : resserre la période pour le "
    "chiffre exact.)":
        {"en": " (The count of documents with a discrepancy is capped: narrow "
               "the period for the exact figure.)"},
    # ------------------------------ 11. régularité d'approvisionnement
    # « Rythme » et jamais « délai » : le serveur ne mesure pas un délai
    # commande -> livraison (aucune date de commande n'existe en base). La
    # traduction anglaise porte la même prudence, sans quoi un lecteur
    # anglophone lirait « lead time » là où il n'y en a pas.
    "Régularité fournisseurs": {"en": "Supplier regularity"},
    "Fournisseurs mesurés": {"en": "Suppliers measured"},
    "Rythme moyen (jours)": {"en": "Average cadence (days)"},
    "Rythme régulier": {"en": "Steady cadence"},
    "Au-delà de leur rythme": {"en": "Past their usual cadence"},
    "Rythme moyen (j)": {"en": "Avg cadence (d)"},
    "Plus court (j)": {"en": "Shortest (d)"},
    "Plus long (j)": {"en": "Longest (d)"},
    "Régulier": {"en": "Steady"},
    "Dernière": {"en": "Last"},
    "Depuis (j)": {"en": "Since (d)"},
    "Écart au rythme (j)": {"en": "Gap to cadence (d)"},
    "Annoncé (j)": {"en": "Stated (d)"},
    "Devise": {"en": "Currency"},
    "Paiement": {"en": "Payment"},
    "Ce rapport mesure le RYTHME d'approvisionnement — le nombre de jours "
    "entre deux réceptions successives d'un même fournisseur — sur {jours} "
    "jours. Ce n'est PAS un délai entre la commande et la livraison : le "
    "système n'enregistre aucune date de commande. La colonne « Annoncé » "
    "est le délai que le fournisseur déclare, saisi à la main sur sa fiche "
    "contact.":
        {"en": "This report measures supply CADENCE — the number of days "
               "between two successive receipts from the same supplier — over "
               "{jours} days. It is NOT a lead time between order and "
               "delivery: the system records no order date. The « Stated » "
               "column is the delay the supplier claims, typed by hand on "
               "their contact record."},
    "Aucune réception rattachée à un fournisseur enregistré sur la période. "
    "Rattache le fournisseur au bon de réception (champ « Fournisseur ») "
    "pour alimenter ce rapport.":
        {"en": "No receipt linked to a registered supplier over the period. "
               "Link the supplier on the receiving document (« Supplier » "
               "field) to feed this report."},
    " {n} réception(s) de la période ne sont rattachées à aucun fournisseur "
    "enregistré et ne sont donc pas comptées ici.":
        {"en": " {n} receipt(s) in the period are not linked to any registered "
               "supplier and are therefore not counted here."},
    # ------------------------------------ fiche fournisseur enrichie
    "WhatsApp": {"en": "WhatsApp"},
    "Délai (j)": {"en": "Delay (d)"},
    "— Informations fournisseur (facultatif) —":
        {"en": "— Supplier information (optional) —"},
    "WhatsApp :": {"en": "WhatsApp:"},
    "Délai habituel (jours) :": {"en": "Usual delay (days):"},
    "Devise :": {"en": "Currency:"},
    "Conditions de paiement :": {"en": "Payment terms:"},
    "Si différent du téléphone": {"en": "If different from the phone number"},
    "Délai annoncé par le fournisseur": {"en": "Delay stated by the supplier"},
    "Ex. HTG, USD": {"en": "E.g. HTG, USD"},
    "Ex. 30 jours, comptant": {"en": "E.g. 30 days, cash"},
    "Le délai doit être un nombre entier de jours (ex. 21). Laisse vide si "
    "tu ne le connais pas.":
        {"en": "The delay must be a whole number of days (e.g. 21). Leave "
               "empty if you do not know it."},
    "Le délai doit être compris entre 0 et 365 jours.":
        {"en": "The delay must be between 0 and 365 days."},
    # ------------------------------------------ fiche « vie d'un produit »
    "\U0001f552  Historique": {"en": "\U0001f552  History"},
    "Historique — {sku}": {"en": "History — {sku}"},
    "Nature": {"en": "Kind"},
    "Évènement": {"en": "Event"},
    "Détail": {"en": "Details"},
    "Fiche": {"en": "Record"},
    "Mouvement": {"en": "Movement"},
    "Aucun mouvement ni changement de fiche pour cet article : il n'a encore "
    "rien vécu.":
        {"en": "No movement and no record change for this item: nothing has "
               "happened to it yet."},
    "{n} évènement(s), du plus récent au plus ancien. En bleu : les "
    "changements de fiche. En gris : les bons annulés, conservés pour que la "
    "chronologie reste complète. Stock actuel : {stock}.":
        {"en": "{n} event(s), most recent first. In blue: record changes. In "
               "grey: cancelled documents, kept so the timeline stays "
               "complete. Current stock: {stock}."},
    "Bon n° {id}": {"en": "Document no. {id}"},
    "{q} {u}": {"en": "{q} {u}"},
    "reçu {q} (écart {e})": {"en": "received {q} (gap {e})"},
    "{source} → {region}": {"en": "{source} → {region}"},
    "réf. {r}": {"en": "ref. {r}"},
}
