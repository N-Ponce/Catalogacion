# Catalogación

Aplicación [Streamlit](https://streamlit.io) para validar la catalogación de
SKUs en `simple.ripley.cl` utilizando la API pública de VTEX. Ingresa una lista
de SKUs y el sistema indicará si cada uno está correctamente ubicado en una
jerarquía de categorías (al menos dos niveles útiles y sin "otros/miscel").

## Instalación

```bash
pip install -r requirements.txt
```

## Uso

```bash
streamlit run APP_CATALOG.py
```

Luego, pega los SKUs en la interfaz web y presiona **Validar catalogación**.

