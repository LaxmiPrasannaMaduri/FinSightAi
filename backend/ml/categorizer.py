"""
categorizer.py
--------------
Runtime inference wrapper around our own trained model
(model.joblib + vectorizer.joblib, produced by train_model.py).

No external LLM/AI API calls happen here -- this loads a local
scikit-learn model file and runs prediction in-process.

Behaviour:
1. If the trained model exists, use it. It returns a category + a
   confidence score (predict_proba).
2. If confidence is below CONFIDENCE_THRESHOLD, fall back to simple
   keyword matching against category_seed.json.
3. If neither matches anything -> "Others".

Self-healing: the model is (re)trained automatically on startup when
  - there is no saved model yet,
  - category_seed.json changed since the model was trained, or
  - the saved model can't be loaded/used (e.g. it was trained with a
    different scikit-learn version than the one installed).
So you never have to remember to run train_model.py by hand.
"""

import hashlib
import json
import re
from pathlib import Path

import joblib

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"

MODEL_PATH = BASE_DIR / "model.joblib"
VECTORIZER_PATH = BASE_DIR / "vectorizer.joblib"
LABELS_PATH = BASE_DIR / "labels.json"
META_PATH = BASE_DIR / "meta.json"
SEED_FILE = DATA_DIR / "category_seed.json"

CONFIDENCE_THRESHOLD = 0.35


def _seed_sha() -> str:
    return hashlib.sha256(SEED_FILE.read_bytes()).hexdigest()[:16]


def _run_training():
    try:
        from . import train_model          # imported as part of the "ml" package
    except ImportError:
        import train_model                 # imported standalone (e.g. from a test script)
    train_model.main()


class Categorizer:
    def __init__(self):
        self._model = None
        self._vectorizer = None
        self._labels = None
        self._seed = None
        self._load()

    # ------------------------------------------------------------------ io
    def _needs_training(self) -> bool:
        if not (MODEL_PATH.exists() and VECTORIZER_PATH.exists() and LABELS_PATH.exists()):
            return True
        try:
            with open(META_PATH) as f:
                return json.load(f).get("seed_sha") != _seed_sha()
        except Exception:
            return True

    def _try_load_model(self) -> bool:
        try:
            model = joblib.load(MODEL_PATH)
            vectorizer = joblib.load(VECTORIZER_PATH)
            with open(LABELS_PATH) as f:
                labels = json.load(f)
            model.predict_proba(vectorizer.transform(["health check"]))  # make sure it really works
        except Exception:
            return False
        self._model, self._vectorizer, self._labels = model, vectorizer, labels
        return True

    def _load(self):
        with open(SEED_FILE) as f:
            self._seed = json.load(f)

        if self._needs_training():
            print("[categorizer] (re)training model from category_seed.json ...")
            _run_training()

        if not self._try_load_model():
            print("[categorizer] saved model unusable (version mismatch?) - retraining ...")
            _run_training()
            if not self._try_load_model():
                print("[categorizer] WARNING: running on keyword rules only.")
                self._model = None

    def reload(self):
        """Call after retraining so the running server picks up the new model."""
        self._load()

    # ------------------------------------------------------------ predict
    @staticmethod
    def _clean(text: str) -> str:
        return re.sub(r"\s+", " ", text.strip())

    def _keyword_fallback(self, description: str):
        desc_lower = description.lower()
        for category, info in self._seed.items():
            for kw in info.get("keywords", []):
                if kw in desc_lower:
                    return category, 0.55, "rule"
        # An unrecognised UPI / IMPS / NEFT counterparty is most likely a person.
        if re.match(r"^(upi|imps|neft|rtgs)[/\- ]", desc_lower):
            return "Transfers (P2P/UPI)", 0.4, "rule"
        return "Others", 0.0, "rule"

    def predict(self, description: str):
        """Returns (category: str, confidence: float, source: "model" | "rule")"""
        description = self._clean(description)

        if self._model is not None:
            X = self._vectorizer.transform([description])
            proba = self._model.predict_proba(X)[0]
            best_idx = proba.argmax()
            confidence = float(proba[best_idx])
            if confidence >= CONFIDENCE_THRESHOLD:
                return str(self._model.classes_[best_idx]), confidence, "model"

        return self._keyword_fallback(description)

    def predict_batch(self, descriptions):
        return [self.predict(d) for d in descriptions]


# module-level singleton, import and reuse
categorizer = Categorizer()
