"""
widget_minimalista.py - Lanzador ligero del Widget Flotante (vista minimalista)
================================================================================
Opción más liviana para cuando solo se quiere la barra minimalista sin abrir
el panel de control completo. Usa exactamente la MISMA clase e interfaz que
el widget integrado en hud_app.py (floating_widget.py) -- cualquier ajuste
visual que se haga ahí (íconos, colores, reloj de autonomía, tamaños, etc.)
se aplica automáticamente aquí también, porque es literalmente el mismo código.

Diferencia con el modo integrado: este script abre su PROPIA conexión
Bluetooth, ya que está pensado para ejecutarse SOLO (sin la app principal
abierta al mismo tiempo). Si la app principal (hud_app.py) ya está corriendo
y conectada, no ejecutes también este script: competirían por el mismo
adaptador Bluetooth. Para usar la vista minimalista JUNTO a la app completa,
abre el "Widget Flotante" desde dentro de hud_app.py en su lugar.
"""

from ble_manager import BluettiBleManager
from floating_widget import FloatingHudWidget, acquire_single_instance_lock, notify_already_running


def main():
    # Si la app completa (hud_app.py) o otra copia de este mismo widget ya
    # está corriendo y conectada, no abrir una segunda conexión Bluetooth:
    # competirían por el mismo equipo (ver acquire_single_instance_lock).
    if not acquire_single_instance_lock():
        notify_already_running()
        return

    ble = BluettiBleManager(default_mac="DC:B4:D9:54:78:1A")

    widget = FloatingHudWidget(master=None, ble=ble, standalone=True)
    widget.show_pill_view()

    ble.on_telemetry = widget.update_from_telemetry

    ble.start()
    try:
        widget.mainloop()
    finally:
        ble.stop()


if __name__ == "__main__":
    main()
