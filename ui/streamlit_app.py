"""NightShift demo UI.

Phase 0: a placeholder page that confirms the UI can reach the API.
The real interface (failure queue, triage card, approval flow) is Phase 7.
"""

import requests
import streamlit as st

from app.config import get_settings

st.set_page_config(page_title="NightShift", layout="wide")

st.title("NightShift")
st.caption("Overnight job triage · Nordwerk Antriebstechnik (pilot)")
st.caption("Demo environment · synthetic data")

api_url = get_settings().api_base_url
try:
    response = requests.get(f"{api_url}/health", timeout=3)
    response.raise_for_status()
    st.success(f"API is up ({api_url}), version {response.json()['version']}.")
except requests.RequestException:
    st.error(f"Cannot reach the API at {api_url}. Start it with `make api`.")
