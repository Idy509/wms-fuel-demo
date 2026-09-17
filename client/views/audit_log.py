import json
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

import customtkinter as ctk

from async_call import run_async
from i18n import t

TOUS_LES_TYPES = "Tout"

# Libellé affiché -> (entity_type, action) tels qu'ils sont RÉELLEMENT écrits
# par `_journaliser_audit()` côté serveur ("" = pas de contrainte).
#
# Les valeurs sont vérifiées une par une sur server/app.py : les seuls
# entity_type existants sont product, document, user, contact, bottle_ledger
# et alerte_regionale. Deux libellés pointaient vers des valeurs qui n'ont
# jamais existé (« thresholds », « bottle_return ») : ces deux filtres
# renvoyaient donc toujours une liste vide. Un changement de seuil, lui,
# n'a pas d'entity_type propre — il est tracé en product/threshold, d'où
# la contrainte sur l'action.
TYPE_LIBELLES = {
    TOUS_LES_TYPES: ("", ""),
    "Produits": ("product", ""),
    "Bons": ("document", ""),
    "Utilisateurs": ("user", ""),
    "Contacts": ("contact", ""),
    "Seuils": ("product", "threshold"),
    "Bouteilles": ("bottle_ledger", ""),
    "Alertes régionales": ("alerte_regionale", ""),
}


class AuditLogView(ctk.CTkFrame):

    def __init__(self, master, api):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self._entries: list[dict] = []
        self._visible: list[dict] = []
        # Libellé affiché (traduit) -> valeur entity_type. Construit ici :
        # t() ne peut pas être appelé au niveau module, la langue n'étant
        # fixée qu'après l'import des vues.
        self._types_affiches = {t(libelle): valeur
                                for libelle, valeur in TYPE_LIBELLES.items()}
        self._tous_les_types = t(TOUS_LES_TYPES)
        self._build()

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 8))
        ctk.CTkLabel(top, text=t("📋  Journal d'audit"),
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")
        ctk.CTkButton(top, text=t("↻  Rafraîchir"), width=110,
                      fg_color="#2a2d35", hover_color="#353840",
                      corner_radius=8, command=self.refresh).pack(side="right", padx=8)
        # Export : le journal n'était consultable qu'à l'écran, donc
        # impossible à joindre à un dossier ou à conserver hors du système.
        self._btn_export = ctk.CTkButton(
            top, text=t("📊  Exporter"), width=120,
            fg_color="#2563eb", hover_color="#1d4ed8",
            corner_radius=8, command=self._exporter)
        self._btn_export.pack(side="right", padx=8)

        # --- Filtres (côté client, comme dans les autres vues) ---
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._render())
        ctk.CTkEntry(top, textvariable=self.search_var,
                     placeholder_text=t("\U0001f50d  Rechercher..."),
                     width=220, corner_radius=8, height=32,
                     fg_color="#1a1d23", border_color="#2a2d35").pack(side="right", padx=8)

        self.type_var = tk.StringVar(value=self._tous_les_types)
        ctk.CTkComboBox(top, variable=self.type_var,
                        values=list(self._types_affiches),
                        width=160, height=32, corner_radius=8,
                        state="readonly",
                        fg_color="#1a1d23", border_color="#2a2d35",
                        button_color="#2a2d35",
                        command=lambda _v: self._render()).pack(side="right", padx=8)

        table_frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                                   border_width=1, border_color="#2a2d35")
        table_frame.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        columns = ("date", "user", "action", "entity", "entity_id", "details")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {"date": t("Date"), "user": t("Utilisateur"),
                    "action": t("Action"), "entity": t("Entité"),
                    "entity_id": "ID", "details": t("Détails")}
        widths = {"date": 150, "user": 120, "action": 100,
                  "entity": 100, "entity_id": 60, "details": 500}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w")

        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=20, pady=(0, 8))
        self._status = ctk.CTkLabel(bottom, text="", font=ctk.CTkFont(size=11),
                                    text_color="#6b7280")
        self._status.pack(side="left")

        detail_frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                                    border_width=1, border_color="#2a2d35")
        detail_frame.pack(fill="x", padx=20, pady=(0, 16))
        ctk.CTkLabel(detail_frame, text=t("Détail de la modification"),
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color="#9ca3af").pack(anchor="w", padx=12, pady=(8, 4))
        self._detail_text = ctk.CTkTextbox(detail_frame, height=120,
                                           fg_color="#111318", text_color="#d1d5db",
                                           font=ctk.CTkFont(family="Consolas", size=11))
        self._detail_text.pack(fill="x", padx=12, pady=(0, 8))

    def refresh(self):
        # 500 = maximum accepté par le serveur ; les filtres ci-dessous
        # rendent ce volume exploitable. L'appel part en tâche de fond : en
        # direct, la fenêtre restait figée le temps du chargement.
        self._status.configure(text=t("Chargement..."), text_color="#6b7280")
        run_async(self, lambda: self.api.get_audit_log(limit=500),
                  self._on_entries, self._on_error)

    def _on_entries(self, entries):
        self._entries = entries
        self._render()

    def _on_error(self, exc):
        self._status.configure(text=str(exc), text_color="orange")

    # ------------------------------------------------------------- export

    def _exporter(self):
        """Télécharge le journal (30 derniers jours par défaut côté serveur),
        puis demande où l'enregistrer.

        Même ordre que l'export comptable : les octets d'abord, la boîte
        « Enregistrer sous » ensuite. Un refus du serveur ne laisse donc
        jamais un fichier vide sur le bureau.

        Le filtre de type actif à l'écran est transmis au serveur : exporter
        « Utilisateurs » depuis un écran filtré sur « Utilisateurs » doit
        donner le même contenu que ce qui est affiché.
        """
        type_voulu, _action = self._types_affiches.get(self.type_var.get(), ("", ""))
        self._btn_export.configure(state="disabled", text=t("Génération..."))

        def _restaurer():
            if self._btn_export.winfo_exists():
                self._btn_export.configure(state="normal", text=t("📊  Exporter"))

        def _ok(contenu: bytes):
            _restaurer()
            chemin = filedialog.asksaveasfilename(
                defaultextension=".xlsx", filetypes=[("Excel", "*.xlsx")],
                initialfile=f"journal_audit_{date.today().strftime('%Y-%m-%d')}.xlsx")
            if not chemin:
                return
            try:
                Path(chemin).write_bytes(contenu)
            except OSError as exc:
                messagebox.showerror(
                    t("Journal d'audit"),
                    t("Impossible d'écrire le fichier :\n{erreur}").format(erreur=exc))
                return
            messagebox.showinfo(
                t("Journal d'audit"),
                t("Fichier enregistré :\n{chemin}").format(chemin=chemin))

        def _err(exc):
            _restaurer()
            messagebox.showerror(t("Journal d'audit"), str(exc))

        run_async(self,
                  lambda: self.api.export_audit_log(entity_type=type_voulu or None),
                  _ok, _err)

    def _filtrer(self) -> list[dict]:
        type_voulu, action_voulue = self._types_affiches.get(
            self.type_var.get(), ("", ""))
        recherche = self.search_var.get().strip().lower()
        resultat = []
        for entry in self._entries:
            if type_voulu and (entry.get("entity_type") or "") != type_voulu:
                continue
            if action_voulue and (entry.get("action") or "") != action_voulue:
                continue
            if recherche:
                blob = " ".join(str(entry.get(k) or "") for k in
                                ("username", "action", "entity_type",
                                 "entity_id", "old_values", "new_values")).lower()
                if recherche not in blob:
                    continue
            resultat.append(entry)
        return resultat

    def _render(self):
        self.tree.delete(*self.tree.get_children())
        self._visible = self._filtrer()
        for i, entry in enumerate(self._visible):
            details = self._format_summary(entry)
            self.tree.insert("", "end", iid=str(i), values=(
                (entry.get("created_at") or "")[:19],
                entry.get("username") or "—",
                entry.get("action") or "",
                entry.get("entity_type") or "",
                entry.get("entity_id") or "",
                details,
            ))
        total = len(self._entries)
        affiche = len(self._visible)
        if affiche == total:
            self._status.configure(text=t("{n} entrée(s)").format(n=total))
        else:
            self._status.configure(
                text=t("{affiche} entrée(s) sur {total}").format(
                    affiche=affiche, total=total))
        self._detail_text.delete("1.0", "end")

    def _format_summary(self, entry: dict) -> str:
        old = entry.get("old_values")
        new = entry.get("new_values")
        if not old and not new:
            return entry.get("action", "")
        try:
            old_d = json.loads(old) if old else {}
            new_d = json.loads(new) if new else {}
        except (json.JSONDecodeError, TypeError):
            return f"{old} → {new}"
        changes = []
        all_keys = set(list(old_d.keys()) + list(new_d.keys()))
        for k in sorted(all_keys):
            ov = old_d.get(k)
            nv = new_d.get(k)
            if ov != nv:
                changes.append(f"{k}: {ov} → {nv}")
        return " | ".join(changes) if changes else "—"

    @staticmethod
    def _formater_valeurs(brut) -> str:
        """JSON indenté si c'en est, sinon la valeur telle quelle."""
        try:
            return json.dumps(json.loads(brut), indent=2, ensure_ascii=False)
        except (json.JSONDecodeError, TypeError, ValueError):
            return str(brut)

    def _on_select(self, _event):
        sel = self.tree.selection()
        if not sel:
            return
        idx = int(sel[0])
        if idx >= len(self._visible):
            return
        entry = self._visible[idx]
        self._detail_text.delete("1.0", "end")
        lines = [
            t("Date     : {valeur}").format(valeur=entry.get("created_at", "")),
            t("Utilisateur : {valeur}").format(valeur=entry.get("username", "—")),
            t("Action   : {valeur}").format(valeur=entry.get("action", "")),
            t("Entité   : {type} #{id}").format(
                type=entry.get("entity_type", ""), id=entry.get("entity_id", "")),
            "",
        ]
        old = entry.get("old_values")
        new = entry.get("new_values")
        # Le json.dumps était DANS le try après un premier append : une valeur
        # non-JSON affichait « Avant : » puis « Avant : {valeur} », deux fois
        # la même chose. Le formatage est fait avant tout affichage.
        if old:
            lines.append(t("Avant :"))
            lines.append(self._formater_valeurs(old))
        if new:
            lines.append("\n" + t("Après :"))
            lines.append(self._formater_valeurs(new))
        self._detail_text.insert("1.0", "\n".join(lines))
