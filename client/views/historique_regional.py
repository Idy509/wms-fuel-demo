"""Historique régional : le journal complet d'UNE région, en un seul écran.

Avant cet écran, l'activité d'une région était éparpillée sur quatre onglets
séparés (Réception, Transfert, Inventaire, Alertes), et chacun ne montrait
que ce qui restait EN COURS — un transfert confirmé, un comptage envoyé la
semaine dernière ou une alerte déjà résolue disparaissaient sans laisser de
trace consultable. Ici, tout ce que la région a fait est réuni et trié par
date, y compris ce qui est terminé.

Quatre sources, fusionnées :
  - `documents`, via `GET /regional/documents` (réceptions, transferts
    envoyés ET reçus, retours) — pas `GET /documents` : cette route générale
    est fermée à un compte régional (middleware `restreindre_comptes_
    regionaux`), qui ne doit voir ni le catalogue central ni l'historique
    d'une autre région. `/regional/documents` élargit en plus le filtre
    serveur à `source_region` en plus de `region` : sans ça, un transfert
    ENVOYÉ par la région n'apparaissait jamais (sa colonne `region` porte la
    destination, pas l'expéditeur).
  - `regional_inventory_reports` (comptages), groupés par horodatage : un
    comptage porte plusieurs lignes (une par produit) qui partagent la même
    date d'envoi, et doivent apparaître comme UN seul événement.
  - `regional_alert_signals` (alertes), avec `historique=True` : sans ce
    drapeau le serveur ne renvoie que les signalements encore ouverts.
"""
from tkinter import ttk

import customtkinter as ctk

from async_call import run_async
from i18n import t


class HistoriqueRegionalView(ctk.CTkFrame):

    def __init__(self, master, api, region: str | None = None,
                 user_role: str | None = None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self.region = region or ""
        self.user_role = user_role or ""
        self._evenements: list[dict] = []
        self._build()

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 8))
        self._titre = ctk.CTkLabel(
            top, text=self._libelle_titre(),
            font=ctk.CTkFont(size=20, weight="bold"), text_color="#f0f0f0")
        self._titre.pack(side="left")
        ctk.CTkButton(top, text=t("↻  Rafraîchir"), width=110,
                      fg_color="#2a2d35", hover_color="#353840",
                      corner_radius=8, command=self.refresh).pack(side="right", padx=8)

        ctk.CTkLabel(
            self,
            text=t("Réceptions, transferts envoyés et reçus, retours, "
                   "comptages et alertes de {region} — tout, y compris ce "
                   "qui est déjà terminé ou résolu.").format(
                       region=self.region or t("ta région")),
            text_color="#6b7280", font=ctk.CTkFont(size=11),
            wraplength=760, justify="left").pack(anchor="w", padx=20, pady=(0, 8))

        cadre = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                             border_width=1, border_color="#2a2d35")
        cadre.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        colonnes = ("date", "type", "detail", "auteur")
        self.tree = ttk.Treeview(cadre, columns=colonnes, show="headings")
        entetes = {"date": t("Date"), "type": t("Type"),
                   "detail": t("Détail"), "auteur": t("Par")}
        largeurs = {"date": 140, "type": 150, "detail": 400, "auteur": 140}
        for col in colonnes:
            self.tree.heading(col, text=entetes[col])
            self.tree.column(col, width=largeurs[col], anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        barre = ttk.Scrollbar(cadre, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=barre.set)
        barre.pack(side="right", fill="y")

        self.tree.tag_configure("transit", foreground="#f59e0b")
        self.tree.tag_configure("annule", foreground="#6b7280")
        self.tree.tag_configure("alerte_ouverte", foreground="#ef4444")
        self.tree.tag_configure("alerte_resolue", foreground="#4ade80")

        self._status = ctk.CTkLabel(self, text="", font=ctk.CTkFont(size=11),
                                    text_color="#6b7280")
        self._status.pack(anchor="w", padx=20, pady=(0, 12))

    def _libelle_titre(self) -> str:
        if self.region:
            return t("🕘  Historique — {region}").format(region=self.region)
        return t("🕘  Mon historique")

    def definir_region(self, region):
        """Appelé par l'écran admin quand il change de région sélectionnée."""
        self.region = region or ""
        self._titre.configure(text=self._libelle_titre())

    def refresh(self):
        self._status.configure(text=t("Chargement…"), text_color="#6b7280")
        if not self.region:
            self._status.configure(
                text=t("Aucune région rattachée à ce compte."), text_color="orange")
            return

        def _charger():
            documents = self.api.get_documents_regionaux(
                region=self.region, limit=200)
            inventaires = self.api.get_inventaires_regionaux(
                region=self.region, limit=200)
            alertes = self.api.get_alertes_regionales(
                region=self.region, historique=True)
            return documents, inventaires, alertes

        run_async(self, _charger, self._afficher,
                  lambda exc: self._status.configure(text=str(exc), text_color="orange"))

    def _afficher(self, resultat):
        documents, inventaires, alertes = resultat
        evenements: list[dict] = []
        evenements.extend(self._evenements_documents(documents))
        evenements.extend(self._evenements_inventaire(inventaires))
        evenements.extend(self._evenements_alertes(alertes))
        evenements.sort(key=lambda e: e["date"], reverse=True)
        self._evenements = evenements

        self.tree.delete(*self.tree.get_children())
        for e in evenements:
            self.tree.insert("", "end", tags=e.get("tags", ()), values=(
                e["date"][:16], e["type"], e["detail"], e.get("auteur", "—")))

        self._status.configure(
            text=t("{n} événement(s).").format(n=len(evenements)),
            text_color="#6b7280")

    def _evenements_documents(self, documents: list[dict]) -> list[dict]:
        evenements = []
        for doc in documents:
            doc_type = doc.get("type")
            annule = bool(doc.get("cancelled_at"))
            lignes = doc.get("lines") or []
            resume = ", ".join(
                f"{l.get('name', '')} ({l.get('quantity', 0):g})" for l in lignes[:3])
            if len(lignes) > 3:
                resume += t(" … +{n}").format(n=len(lignes) - 3)
            resume = resume or t("aucune ligne")

            if doc_type == "REGIONAL_TRANSFER" and doc.get("source_region") == self.region:
                libelle = t("Transfert envoyé → {dst}").format(
                    dst=doc.get("region") or "—")
                tag = "annule" if annule else (
                    None if doc.get("received_at") else "transit")
            elif doc_type == "REGIONAL_TRANSFER":
                libelle = t("Transfert reçu de {src}").format(
                    src=doc.get("source_region") or "—")
                tag = "annule" if annule else (
                    None if doc.get("received_at") else "transit")
            elif doc_type == "DELIVERY":
                libelle = t("Réception depuis le central")
                tag = "annule" if annule else (
                    None if doc.get("received_at") else "transit")
            elif doc_type == "RETURN":
                libelle = t("Retour vers le central")
                tag = "annule" if annule else None
            else:
                libelle = doc_type or "—"
                tag = "annule" if annule else None

            if annule:
                libelle = t("{libelle} (annulé)").format(libelle=libelle)

            evenements.append({
                "date": doc.get("created_at") or "",
                "type": libelle,
                "detail": resume,
                "auteur": doc.get("created_by") or "—",
                "tags": (tag,) if tag else (),
            })
        return evenements

    def _evenements_inventaire(self, inventaires: dict) -> list[dict]:
        rapports = (inventaires or {}).get("rapports") or []
        # Un comptage porte N lignes (une par produit) partageant le même
        # horodatage : regroupées ici en UN seul événement, sinon un comptage
        # de 40 produits envahirait le journal de 40 lignes identiques.
        groupes: dict[tuple[str, str], list[dict]] = {}
        for r in rapports:
            cle = (r.get("created_at") or "", r.get("created_by") or "")
            groupes.setdefault(cle, []).append(r)

        evenements = []
        for (date_, auteur), lignes in groupes.items():
            resume = ", ".join(
                f"{l.get('name', '')} ({l.get('quantity', 0):g})" for l in lignes[:3])
            if len(lignes) > 3:
                resume += t(" … +{n}").format(n=len(lignes) - 3)
            evenements.append({
                "date": date_,
                "type": t("Comptage envoyé"),
                "detail": t("{n} article(s) : {resume}").format(
                    n=len(lignes), resume=resume or "—"),
                "auteur": auteur or "—",
            })
        return evenements

    def _evenements_alertes(self, alertes: dict) -> list[dict]:
        lignes = (alertes or {}).get("alertes") or []
        evenements = []
        for a in lignes:
            nom = a.get("name") or a.get("sku") or "—"
            evenements.append({
                "date": a.get("created_at") or "",
                "type": t("Alerte signalée"),
                "detail": t("Rupture signalée : {nom}").format(nom=nom)
                          + (f" — {a['note']}" if a.get("note") else ""),
                "auteur": a.get("created_by") or "—",
                "tags": ("alerte_ouverte",) if not a.get("resolved_at") else (),
            })
            if a.get("resolved_at"):
                evenements.append({
                    "date": a["resolved_at"],
                    "type": t("Alerte résolue"),
                    "detail": t("Rupture résolue : {nom}").format(nom=nom),
                    "auteur": a.get("resolved_by") or "—",
                    "tags": ("alerte_resolue",),
                })
        return evenements
