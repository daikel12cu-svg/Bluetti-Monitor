"""
widget_minimalista.py - Widget Flotante Always-on-Top para Windows
===================================================================
- Fondo con Opacidad / Translucidez (Solo el fondo oscuro se vuelve translúcido; los textos e iconos son 100% sólidos y nítidos).
- DC Output es estrictamente 0W cuando los puertos DC están apagados.
- Menú con clic derecho (Opacidad de fondo, Zoom/Tamaño, Posición).
- Rueda del ratón para regular opacidad del fondo sobre la marcha.
- Teclas [+] y [-] para zoom.
- Always-on-Top real y arrastre suave con el ratón.
- 100% Offline.
"""

import sys
import os
import tkinter as tk
from tkinter import Menu
import time
from typing import Dict, Any
from ble_manager import BluettiBleManager


class MinimalistWidget(tk.Tk):
    def __init__(self):
        super().__init__()

        self.alpha_val = 0.85
        self.scale_factor = 1.0

        # Configuración de ventana de widget flotante
        self.title("Bluetti Minimal Widget")
        self.overrideredirect(True)  # Frameless (sin marcos de ventana de Windows)
        self.attributes("-topmost", True)  # Always on top
        
        # Color del contenedor
        self.bg_color = "#020617"
        self.border_color = "#334155"
        self.configure(bg=self.bg_color)

        # Arrastre con el ratón
        self._drag_x = 0
        self._drag_y = 0

        # Contenedor tipo píldora idéntico a la web
        self.frame = tk.Frame(
            self,
            bg=self.bg_color,
            highlightbackground=self.border_color,
            highlightthickness=1,
            padx=10,
            pady=4
        )
        self.frame.pack(fill="both", expand=True)

        # 1. Indicador de Batería
        self.lbl_bat_icon = tk.Label(
            self.frame,
            text="🔋",
            font=("Segoe UI Emoji", int(10 * self.scale_factor)),
            bg=self.bg_color,
            fg="#10b981"
        )
        self.lbl_bat_icon.pack(side="left", padx=(2, 2))

        self.lbl_bat_title = tk.Label(
            self.frame,
            text="Batería",
            font=("Segoe UI", int(9 * self.scale_factor), "bold"),
            bg=self.bg_color,
            fg="#cbd5e1"
        )
        self.lbl_bat_title.pack(side="left", padx=(0, 4))

        self.lbl_soc = tk.Label(
            self.frame,
            text="--%",
            font=("Segoe UI", int(10 * self.scale_factor), "bold"),
            bg=self.bg_color,
            fg="#10b981"
        )
        self.lbl_soc.pack(side="left", padx=(0, 6))

        # Separador vertical
        self.sep = tk.Frame(self.frame, width=1, height=int(14 * self.scale_factor), bg="#475569")
        self.sep.pack(side="left", padx=4)

        # 2. Indicador de Estado / Potencia Neta (Cargando +W / Descargando -W / Neutro 0W)
        self.lbl_status = tk.Label(
            self.frame,
            text="Estado: Conectando...",
            font=("Segoe UI", int(9 * self.scale_factor), "bold"),
            bg=self.bg_color,
            fg="#38bdf8"
        )
        self.lbl_status.pack(side="left", padx=(4, 6))

        # 3. Botón de ajustes (⚙)
        self.btn_settings = tk.Label(
            self.frame,
            text="⚙",
            font=("Segoe UI", int(9 * self.scale_factor)),
            bg=self.bg_color,
            fg="#94a3b8",
            cursor="hand2"
        )
        self.btn_settings.pack(side="right", padx=(2, 2))
        self.btn_settings.bind("<Button-1>", self._show_context_menu)

        # 4. Botón de cerrar (×)
        self.btn_close = tk.Label(
            self.frame,
            text="×",
            font=("Segoe UI", int(11 * self.scale_factor), "bold"),
            bg=self.bg_color,
            fg="#94a3b8",
            cursor="hand2"
        )
        self.btn_close.pack(side="right", padx=(2, 4))
        self.btn_close.bind("<Button-1>", lambda e: self.destroy())

        # Crear menú contextual de clic derecho
        self._build_context_menu()

        # Vincular eventos de arrastre, clic derecho y rueda del ratón
        for widget in (self, self.frame, self.lbl_bat_icon, self.lbl_bat_title, self.lbl_soc, self.lbl_status, self.sep):
            widget.bind("<ButtonPress-1>", self._start_drag)
            widget.bind("<B1-Motion>", self._do_drag)
            widget.bind("<Button-3>", self._show_context_menu)
            widget.bind("<MouseWheel>", self._on_mouse_wheel)

        self.bind("<plus>", lambda e: self.set_scale(self.scale_factor + 0.1))
        self.bind("<minus>", lambda e: self.set_scale(self.scale_factor - 0.1))
        self.bind("<KP_Add>", lambda e: self.set_scale(self.scale_factor + 0.1))
        self.bind("<KP_Subtract>", lambda e: self.set_scale(self.scale_factor - 0.1))

        # Aplicar opacidad y posición
        self._apply_geometry()
        self.attributes("-alpha", self.alpha_val)

        # Iniciar motor BLE
        self.ble = BluettiBleManager(default_mac="DC:B4:D9:54:78:1A")
        self.ble.on_telemetry = self._on_telemetry
        self.ble.on_status = self._on_status
        self.ble.start()

    def _build_context_menu(self):
        self.context_menu = Menu(self, tearoff=0, bg="#0f172a", fg="#f8fafc", activebackground="#0284c7", activeforeground="#ffffff")
        
        # Submenú Opacidad
        opacity_menu = Menu(self.context_menu, tearoff=0, bg="#0f172a", fg="#f8fafc", activebackground="#0284c7")
        for op in (100, 95, 90, 85, 80, 70, 60, 50):
            opacity_menu.add_command(label=f"{op}% Opacidad", command=lambda v=op/100.0: self.set_opacity(v))
        self.context_menu.add_cascade(label="Translucidez de Fondo", menu=opacity_menu)

        # Submenú Tamaño
        size_menu = Menu(self.context_menu, tearoff=0, bg="#0f172a", fg="#f8fafc", activebackground="#0284c7")
        for sc, name in ((0.8, "80% (Compacto)"), (1.0, "100% (Normal)"), (1.2, "120% (Grande)"), (1.4, "140% (Ultra)")):
            size_menu.add_command(label=name, command=lambda v=sc: self.set_scale(v))
        self.context_menu.add_cascade(label="Tamaño / Escala", menu=size_menu)

        # Submenú Posición en Pantalla
        pos_menu = Menu(self.context_menu, tearoff=0, bg="#0f172a", fg="#f8fafc", activebackground="#0284c7")
        pos_menu.add_command(label="Arriba a la Derecha", command=lambda: self.set_position("top-right"))
        pos_menu.add_command(label="Arriba al Centro", command=lambda: self.set_position("top-center"))
        pos_menu.add_command(label="Arriba a la Izquierda", command=lambda: self.set_position("top-left"))
        pos_menu.add_separator()
        pos_menu.add_command(label="Abajo a la Derecha", command=lambda: self.set_position("bottom-right"))
        pos_menu.add_command(label="Abajo al Centro", command=lambda: self.set_position("bottom-center"))
        pos_menu.add_command(label="Abajo a la Izquierda", command=lambda: self.set_position("bottom-left"))
        self.context_menu.add_cascade(label="Posición en Pantalla", menu=pos_menu)

        self.context_menu.add_separator()
        self.context_menu.add_command(label="Cerrar Widget", command=self.destroy)

    def _show_context_menu(self, event):
        try:
            self.context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.context_menu.grab_release()

    def set_opacity(self, alpha: float):
        self.alpha_val = max(0.35, min(1.0, alpha))
        self.attributes("-alpha", self.alpha_val)

    def _on_mouse_wheel(self, event):
        if event.delta > 0:
            self.set_opacity(self.alpha_val + 0.05)
        else:
            self.set_opacity(self.alpha_val - 0.05)

    def set_scale(self, scale: float):
        self.scale_factor = max(0.75, min(1.6, scale))
        self.lbl_bat_icon.configure(font=("Segoe UI Emoji", max(8, int(10 * self.scale_factor))))
        self.lbl_bat_title.configure(font=("Segoe UI", max(8, int(9 * self.scale_factor)), "bold"))
        self.lbl_soc.configure(font=("Segoe UI", max(8, int(10 * self.scale_factor)), "bold"))
        self.lbl_status.configure(font=("Segoe UI", max(8, int(9 * self.scale_factor)), "bold"))
        self.btn_close.configure(font=("Segoe UI", max(9, int(11 * self.scale_factor)), "bold"))
        self.btn_settings.configure(font=("Segoe UI", max(8, int(9 * self.scale_factor))))
        self.sep.configure(height=max(10, int(14 * self.scale_factor)))
        self._apply_geometry()

    def _apply_geometry(self, pos: str = "top-right"):
        screen_w = self.winfo_screenwidth()
        w = int(350 * self.scale_factor)
        h = int(36 * self.scale_factor)

        curr_x = self.winfo_x()
        curr_y = self.winfo_y()

        if curr_x <= 0 or curr_y <= 0:
            x = screen_w - w - 24
            y = 20
        else:
            x = curr_x
            y = curr_y

        self.geometry(f"{w}x{h}+{x}+{y}")

    def set_position(self, pos: str):
        screen_w = self.winfo_screenwidth()
        screen_h = self.winfo_screenheight()
        w = int(350 * self.scale_factor)
        h = int(36 * self.scale_factor)

        if pos == "top-right":
            x = screen_w - w - 24
            y = 20
        elif pos == "top-center":
            x = (screen_w - w) // 2
            y = 20
        elif pos == "top-left":
            x = 24
            y = 20
        elif pos == "bottom-right":
            x = screen_w - w - 24
            y = screen_h - h - 60
        elif pos == "bottom-center":
            x = (screen_w - w) // 2
            y = screen_h - h - 60
        elif pos == "bottom-left":
            x = 24
            y = screen_h - h - 60
        else:
            x = screen_w - w - 24
            y = 20

        self.geometry(f"{w}x{h}+{x}+{y}")

    def _start_drag(self, event):
        self._drag_x = event.x_root - self.winfo_x()
        self._drag_y = event.y_root - self.winfo_y()

    def _do_drag(self, event):
        new_x = event.x_root - self._drag_x
        new_y = event.y_root - self._drag_y
        self.geometry(f"+{new_x}+{new_y}")

    def _on_status(self, msg: str):
        self.after(0, lambda: self._update_status_text(msg))

    def _update_status_text(self, msg: str):
        if not self.ble.state.get("connected", False):
            self.lbl_status.configure(text=f"{msg}", fg="#fbbf24")

    def _on_telemetry(self, t: Dict[str, Any]):
        self.after(0, lambda: self._update_ui(t))

    def _update_ui(self, t: Dict[str, Any]):
        soc = t.get("soc", 0)
        # Salida DC es estrictamente 0W si dc_output_on es False
        dc_out_w = t.get("dc_output_watts", 0) if t.get("dc_output_on", False) else 0
        in_w = t.get("dc_input_watts", 0) + t.get("ac_input_watts", 0)
        out_w = t.get("ac_output_watts", 0) + dc_out_w
        net = in_w - out_w

        soc_color = "#10b981" if soc > 50 else ("#fbbf24" if soc > 20 else "#f43f5e")
        self.lbl_soc.configure(text=f"{soc}%", fg=soc_color)

        if net > 0:
            status_text = f"Estado: Cargando +{net}W"
            status_color = "#10b981"
        elif net < 0:
            status_text = f"Estado: Descargando {abs(net)}W"
            status_color = "#f59e0b"
        else:
            status_text = "Estado: Neutro 0W"
            status_color = "#cbd5e1"

        self.lbl_status.configure(text=status_text, fg=status_color)


def main():
    app = MinimalistWidget()
    try:
        app.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        if hasattr(app, "ble"):
            app.ble.stop()


if __name__ == "__main__":
    main()
