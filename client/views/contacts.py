import tkinter as tk
from tkinter import messagebox, ttk

import customtkinter as ctk

from async_call import run_async
from i18n import t


TYPE_LABELS = {"supplier": "Fournisseur", "customer": "Client", "other": "Autre"}
TYPE_VALUES = ["supplier", "customer", "other"]

# Les listes deroulantes affichent le libelle francais ; le serveur attend la
# valeur interne. Ces deux helpers font la conversion dans les deux sens.
def type_libelles() -> list[str]:
    """Libellés affichés, dans l'ordre de TYPE_VALUES et dans la langue
    courante. Calculés à l'appel : la langue n'est fixée qu'après l'import
    des vues, une liste construite au niveau module resterait en français."""
    return [libelle_type(v) for v in TYPE_VALUES]


def libelle_type(valeur: str) -> str:
    """Valeur interne -> libelle affiche."""
    return t(TYPE_LABELS.get(valeur, TYPE_LABELS["other"]))


def type_depuis_libelle(libelle: str) -> str:
    """Libelle affiche -> valeur interne envoyee au serveur."""
    for valeur in TYPE_VALUES:
        if libelle_type(valeur) == libelle:
            return valeur
    return libelle if libelle in TYPE_VALUES else "other"


class ContactsView(ctk.CTkFrame):

    def __init__(self, master, api, user_role: str | None = None):
        super().__init__(master, fg_color="transparent")
        self.api = api
        self._user_role = user_role
        self._contacts: list[dict] = []
        self._build()

    def _build(self):
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=20, pady=(20, 8))
        ctk.CTkLabel(top, text=t("\U0001f4c7  Contacts (Fournisseurs / Clients)"),
                     font=ctk.CTkFont(size=20, weight="bold"),
                     text_color="#f0f0f0").pack(side="left")
        ctk.CTkButton(top, text=t("↻  Rafraîchir"), width=110,
                      fg_color="#2a2d35", hover_color="#353840",
                      corner_radius=8, command=self.refresh).pack(side="right", padx=8)

        # --- Barre de recherche ---
        self.search_var = tk.StringVar()
        self.search_var.trace_add("write", lambda *_: self._render())
        ctk.CTkEntry(top, textvariable=self.search_var,
                     placeholder_text=t("\U0001f50d  Rechercher..."),
                     width=240, corner_radius=8, height=32,
                     fg_color="#1a1d23", border_color="#2a2d35").pack(side="right", padx=8)

        form = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                            border_width=1, border_color="#2a2d35")
        form.pack(fill="x", padx=20, pady=8)
        ctk.CTkLabel(form, text=t("Ajouter un contact"),
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color="#f0f0f0").grid(
            row=0, column=0, columnspan=6, sticky="w", padx=8, pady=(10, 4))

        self.name_var = tk.StringVar()
        self.type_var = tk.StringVar(value=libelle_type("supplier"))
        self.phone_var = tk.StringVar()
        self.email_var = tk.StringVar()
        self.address_var = tk.StringVar()

        ctk.CTkEntry(form, textvariable=self.name_var,
                     placeholder_text=t("Nom *"), width=180).grid(
            row=1, column=0, padx=6, pady=8)
        ctk.CTkComboBox(form, variable=self.type_var,
                        values=type_libelles(),
                        width=120, state="readonly").grid(
            row=1, column=1, padx=6, pady=8)
        ctk.CTkEntry(form, textvariable=self.phone_var,
                     placeholder_text=t("Téléphone"), width=130).grid(
            row=1, column=2, padx=6, pady=8)
        ctk.CTkEntry(form, textvariable=self.email_var,
                     placeholder_text=t("Email"), width=160).grid(
            row=1, column=3, padx=6, pady=8)
        ctk.CTkEntry(form, textvariable=self.address_var,
                     placeholder_text=t("Adresse"), width=200).grid(
            row=1, column=4, padx=6, pady=8)
        self._btn_ajouter = ctk.CTkButton(form, text=t("Ajouter"), width=80,
                                          command=self._create)
        self._btn_ajouter.grid(row=1, column=5, padx=6, pady=8)


        # Label inline pour erreurs de validation
        self._form_error = ctk.CTkLabel(form, text="", text_color="orange",
                                        font=ctk.CTkFont(size=11))
        self._form_error.grid(row=2, column=0, columnspan=6, sticky="w", padx=8, pady=(0, 4))

        if self._user_role == "lecteur":
            # Un lecteur qui remplit ce formulaire se prenait un 403 à la
            # validation : la lecture seule doit être visible avant l'envoi.
            self._btn_ajouter.configure(state="disabled")
            self._form_error.configure(
                text=t("Lecture seule : un lecteur ne peut pas créer de contacts"),
                text_color="#6b7280")

        table_frame = ctk.CTkFrame(self, fg_color="#1a1d23", corner_radius=10,
                                   border_width=1, border_color="#2a2d35")
        table_frame.pack(fill="both", expand=True, padx=20, pady=(0, 8))

        # Les colonnes fournisseur (WhatsApp, délai, devise) figurent dans le
        # tableau et non seulement dans la fiche : l'intérêt d'un numéro
        # WhatsApp est de pouvoir le lire d'un coup d'œil quand il faut
        # relancer quelqu'un, sans ouvrir chaque contact un par un.
        columns = ("name", "type", "phone", "whatsapp", "email", "address",
                   "delai", "devise", "active")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {"name": t("Nom"), "type": t("Type"), "phone": t("Téléphone"),
                    "whatsapp": t("WhatsApp"),
                    "email": t("Email"), "address": t("Adresse"),
                    "delai": t("Délai (j)"), "devise": t("Devise"),
                    "active": t("Actif")}
        widths = {"name": 180, "type": 100, "phone": 120, "whatsapp": 120,
                  "email": 160, "address": 170, "delai": 80, "devise": 70,
                  "active": 70}
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], anchor="w")

        self.tree.tag_configure("inactive", foreground="#6b7280")
        self.tree.pack(side="left", fill="both", expand=True)
        scrollbar = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")

        # Double-clic pour editer
        self.tree.bind("<Double-1>", self._on_double_click)

        bottom = ctk.CTkFrame(self, fg_color="transparent")
        bottom.pack(fill="x", padx=20, pady=(0, 16))
        self._status = ctk.CTkLabel(bottom, text="", font=ctk.CTkFont(size=11))
        self._status.pack(side="left")

        btn_frame = ctk.CTkFrame(bottom, fg_color="transparent")
        btn_frame.pack(side="right")
        self._btn_toggle = ctk.CTkButton(
            btn_frame, text=t("Désactiver / Réactiver"), width=160,
            fg_color="#7a2a2a", hover_color="#943535",
            command=self._toggle_active)
        self._btn_toggle.pack(side="left", padx=4)
        if self._user_role == "lecteur":
            self._btn_toggle.configure(state="disabled")

    def _erreur(self, exc):
        """Affiche une erreur d'appel serveur sans figer la fenêtre."""
        if self.winfo_exists():
            self._status.configure(text=str(exc), text_color="orange")

    def refresh(self):
        # Tous les appels serveur de cet écran passent par run_async : exécutés
        # sur la boucle Tk, ils gelaient toute l'application le temps du
        # timeout réseau.
        self._status.configure(text=t("Chargement…"), text_color="#6b7280")

        def _ok(contacts):
            self._contacts = contacts
            self._render()

        run_async(self, lambda: self.api.get_contacts(include_inactive=True),
                  _ok, self._erreur)

    def _render(self):
        query = self.search_var.get().strip().lower()
        self.tree.delete(*self.tree.get_children())
        shown = 0
        for c in self._contacts:
            if query:
                searchable = " ".join([
                    c.get("name", ""),
                    libelle_type(c.get("type", "")),
                    c.get("phone") or "",
                    c.get("whatsapp") or "",
                    c.get("email") or "",
                ]).lower()
                if query not in searchable:
                    continue
            tags = () if c.get("active", True) else ("inactive",)
            delai = c.get("delai_jours")
            self.tree.insert("", "end", iid=str(c["id"]), tags=tags, values=(
                c["name"],
                libelle_type(c.get("type", "")),
                c.get("phone") or "",
                c.get("whatsapp") or "",
                c.get("email") or "",
                c.get("address") or "",
                "" if delai is None else str(delai),
                c.get("devise") or "",
                t("Oui") if c.get("active", True) else t("Non"),
            ))
            shown += 1
        total = len(self._contacts)
        if query and shown != total:
            self._status.configure(
                text=t("{shown} / {total} contact(s)").format(
                    shown=shown, total=total),
                                   text_color="#6b7280")
        else:
            self._status.configure(text=t("{n} contact(s)").format(n=total),
                                   text_color="#6b7280")

    def _create(self):
        name = self.name_var.get().strip()
        if not name:
            self._form_error.configure(text=t("Le nom est obligatoire"))
            return
        self._form_error.configure(text="")
        kwargs = {}
        phone = self.phone_var.get().strip()
        email = self.email_var.get().strip()
        address = self.address_var.get().strip()
        if phone:
            kwargs["phone"] = phone
        if email:
            kwargs["email"] = email
        if address:
            kwargs["address"] = address
        # Bouton verrouillé pendant l'envoi : sans cela un double clic part
        # deux fois et la fenêtre reste figée le temps du réseau.
        self._btn_ajouter.configure(state="disabled", text=t("Envoi…"))
        self._status.configure(text=t("Création en cours…"), text_color="#6b7280")

        def _restaurer():
            if self._btn_ajouter.winfo_exists():
                self._btn_ajouter.configure(state="normal", text=t("Ajouter"))

        def _ok(result):
            _restaurer()
            if result.get("already_exists"):
                self._form_error.configure(
                    text=t("Contact « {nom} » existe déjà").format(nom=name))
                self._status.configure(text="")
                return
            self.name_var.set("")
            self.phone_var.set("")
            self.email_var.set("")
            self.address_var.set("")
            self.refresh()

        def _err(exc):
            _restaurer()
            self._status.configure(text="")
            messagebox.showerror("Erreur", str(exc))

        run_async(self,
                  lambda: self.api.create_contact(
                      name, type_depuis_libelle(self.type_var.get()), **kwargs),
                  _ok, _err)

    # WebSocket methods for real-time updates
    def contact_cree(self, contact_data):
        """Handle a new contact created event from WebSocket."""
        self._contacts.append(contact_data)
        self._render()

    def contact_mis_a_jour(self, contact_data):
        """Handle a contact updated event from WebSocket."""
        for i, c in enumerate(self._contacts):
            if c["id"] == contact_data["id"]:
                self._contacts[i] = contact_data
                break
        self._render()

    def contact_supprime(self, contact_id):
        """Handle a contact deleted event from WebSocket."""
        self._contacts = [c for c in self._contacts if c.get("id") != contact_id]
        self._render()

    def _selected_contact(self) -> dict | None:
        sel = self.tree.selection()
        if not sel:
            self._status.configure(text=t("Sélectionne un contact"),
                                   text_color="orange")
            return None
        cid = int(sel[0])
        for c in self._contacts:
            if c["id"] == cid:
                return c
        return None

    def _toggle_active(self):
        contact = self._selected_contact()
        if not contact:
            return
        new_state = not contact.get("active", True)
        question = (t("Réactiver « {nom} » ?") if new_state
                    else t("Désactiver « {nom} » ?"))
        if not messagebox.askyesno(t("Confirmation"),
                                   question.format(nom=contact["name"])):
            return
        self._btn_toggle.configure(state="disabled", text=t("Envoi…"))
        self._status.configure(text=t("Enregistrement…"), text_color="#6b7280")

        def _restaurer():
            if self._btn_toggle.winfo_exists():
                self._btn_toggle.configure(state="normal",
                                           text=t("Désactiver / Réactiver"))

        def _ok(_res):
            _restaurer()
            self.refresh()

        def _err(exc):
            _restaurer()
            self._status.configure(text="")
            messagebox.showerror("Erreur", str(exc))

        run_async(self,
                  lambda: self.api.update_contact(contact["id"], active=new_state),
                  _ok, _err)

    def _on_double_click(self, event):
        if self._user_role == "lecteur":
            self._status.configure(
                text=t("Lecture seule : un lecteur ne peut pas modifier de "
                       "contacts"),
                                   text_color="#6b7280")
            return
        sel = self.tree.selection()
        if not sel:
            return
        contact = self._selected_contact()
        if not contact:
            return
        EditContactDialog(self, self.api, contact, on_saved=self.refresh)


class EditContactDialog(ctk.CTkToplevel):
    def __init__(self, master, api, contact: dict, on_saved=None):
        super().__init__(master)
        self.api = api
        self.contact = contact
        self.on_saved = on_saved
        self.title(t("Modifier — {nom}").format(nom=contact["name"]))
        # Fenêtre agrandie : quatre champs fournisseur se sont ajoutés sous
        # les champs d'origine. Sans cet agrandissement, les boutons
        # « Annuler / Enregistrer » sortaient du cadre et la fiche devenait
        # impossible à valider.
        self.geometry("520x560")
        self.resizable(False, False)

        self.update_idletasks()
        x = master.winfo_rootx() + 100
        y = master.winfo_rooty() + 50
        self.geometry(f"520x560+{x}+{y}")

        self.transient(master)
        self.lift()
        self.grab_set()

        ctk.CTkLabel(self, text=t("Modifier le contact"),
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=(16, 12))

        form = ctk.CTkFrame(self, fg_color="transparent")
        form.pack(fill="x", padx=30)

        self.name_var = tk.StringVar(value=contact["name"])
        self.type_var = tk.StringVar(value=libelle_type(contact.get("type", "other")))
        self.phone_var = tk.StringVar(value=contact.get("phone") or "")
        self.email_var = tk.StringVar(value=contact.get("email") or "")
        self.address_var = tk.StringVar(value=contact.get("address") or "")
        # Champs fournisseur. Tous facultatifs : la même fiche sert aux
        # clients, pour qui aucun n'a de sens.
        self.whatsapp_var = tk.StringVar(value=contact.get("whatsapp") or "")
        delai = contact.get("delai_jours")
        self.delai_var = tk.StringVar(value="" if delai is None else str(delai))
        self.devise_var = tk.StringVar(value=contact.get("devise") or "")
        self.conditions_var = tk.StringVar(
            value=contact.get("conditions_paiement") or "")

        row = 0
        for label, var, width in [
            ("Nom :", self.name_var, 260),
            ("Téléphone :", self.phone_var, 200),
            ("Email :", self.email_var, 260),
            ("Adresse :", self.address_var, 260),
        ]:
            ctk.CTkLabel(form, text=t(label)).grid(row=row, column=0,
                                                   sticky="w", pady=4)
            ctk.CTkEntry(form, textvariable=var, width=width).grid(
                row=row, column=1, padx=8, pady=4, sticky="w")
            row += 1

        ctk.CTkLabel(form, text=t("Type :")).grid(row=row, column=0,
                                                  sticky="w", pady=4)
        ctk.CTkComboBox(form, variable=self.type_var, values=type_libelles(),
                        width=140, state="readonly").grid(
            row=row, column=1, padx=8, pady=4, sticky="w")
        row += 1

        ctk.CTkLabel(form, text=t("— Informations fournisseur (facultatif) —"),
                     text_color="#6b7280",
                     font=ctk.CTkFont(size=11)).grid(
            row=row, column=0, columnspan=2, sticky="w", pady=(12, 2))
        row += 1

        for label, var, width, aide in [
            ("WhatsApp :", self.whatsapp_var, 200,
             t("Si différent du téléphone")),
            ("Délai habituel (jours) :", self.delai_var, 90,
             t("Délai annoncé par le fournisseur")),
            ("Devise :", self.devise_var, 90, t("Ex. HTG, USD")),
            ("Conditions de paiement :", self.conditions_var, 200,
             t("Ex. 30 jours, comptant")),
        ]:
            ctk.CTkLabel(form, text=t(label)).grid(row=row, column=0,
                                                   sticky="w", pady=4)
            cellule = ctk.CTkFrame(form, fg_color="transparent")
            cellule.grid(row=row, column=1, padx=8, pady=4, sticky="w")
            ctk.CTkEntry(cellule, textvariable=var, width=width).pack(side="left")
            ctk.CTkLabel(cellule, text=aide, text_color="#6b7280",
                         font=ctk.CTkFont(size=10)).pack(side="left", padx=(6, 0))
            row += 1

        self.status_lbl = ctk.CTkLabel(self, text="", text_color="orange")
        self.status_lbl.pack()

        btn_frame = ctk.CTkFrame(self, fg_color="transparent")
        btn_frame.pack(pady=8)
        ctk.CTkButton(btn_frame, text=t("Annuler"), width=100,
                      fg_color="#2a2d35", hover_color="#353840",
                      command=self.destroy).pack(side="left", padx=8)
        self._btn_save = ctk.CTkButton(btn_frame, text=t("Enregistrer"), width=140,
                                       fg_color="#2563eb", hover_color="#1d4ed8",
                                       command=self._save)
        self._btn_save.pack(side="left", padx=8)

    def _save(self):
        name = self.name_var.get().strip()
        if not name:
            self.status_lbl.configure(text=t("Le nom est obligatoire"))
            return
        # Délai validé côté client AVANT l'envoi : un magasinier qui tape
        # « trois semaines » doit lire un message clair sous le champ, pas
        # attendre un aller-retour réseau pour un 422. Le serveur revalide de
        # son côté — c'est lui qui fait autorité.
        delai_texte = self.delai_var.get().strip()
        delai = None
        if delai_texte:
            try:
                delai = int(delai_texte)
            except ValueError:
                self.status_lbl.configure(
                    text=t("Le délai doit être un nombre entier de jours "
                           "(ex. 21). Laisse vide si tu ne le connais pas."))
                return
            if delai < 0 or delai > 365:
                self.status_lbl.configure(
                    text=t("Le délai doit être compris entre 0 et 365 jours."))
                return

        payload = {
            "name": name,
            "type": type_depuis_libelle(self.type_var.get()),
            "phone": self.phone_var.get().strip() or None,
            "email": self.email_var.get().strip() or None,
            "address": self.address_var.get().strip() or None,
            "whatsapp": self.whatsapp_var.get().strip() or None,
            "delai_jours": delai,
            "devise": self.devise_var.get().strip() or None,
            "conditions_paiement": self.conditions_var.get().strip() or None,
        }
        # Comme partout ailleurs dans l'app : appel serveur via run_async, bouton
        # verrouillé pendant l'envoi (sinon la fenêtre gèle et un double clic
        # envoie deux fois).
        self._btn_save.configure(state="disabled", text=t("Envoi…"))
        self.status_lbl.configure(text=t("Enregistrement…"), text_color="#6b7280")

        def _restaurer():
            if self._btn_save.winfo_exists():
                self._btn_save.configure(state="normal", text=t("Enregistrer"))

        def _ok(_res):
            _restaurer()
            if self.on_saved:
                self.on_saved()
            self.destroy()

        def _err(exc):
            _restaurer()
            self.status_lbl.configure(text="", text_color="orange")
            messagebox.showerror("Erreur", str(exc))

        run_async(self,
                  lambda: self.api.update_contact(self.contact["id"], **payload),
                  _ok, _err)
