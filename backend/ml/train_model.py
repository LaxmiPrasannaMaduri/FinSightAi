"""
train_model.py
--------------
Builds and trains our OWN transaction-categorization model.

No external LLM / third-party AI API is used anywhere in this pipeline.
This is a classic, self-contained supervised ML model:

    raw description text
        -> TF-IDF (character n-gram) vectorizer
        -> SGDClassifier (linear model, trained with log loss => gives probabilities)

Why this design:
- Bank / UPI transaction descriptions are short, noisy, abbreviation-heavy
  strings like "UPI-SWIGGY-swiggy@ok" or "NEFT/XXXX/RENT PAYMENT".
- Character n-grams (3-5 chars) let the model recognize sub-strings like
  "SWIG", "ZOMA", "UPI-" regardless of the random order IDs / suffixes
  banks append, which is exactly the kind of noise real statements have.
- We bootstrap an initial labelled dataset from backend/data/category_seed.json
  (keyword lists + realistic templates), which gives the model enough
  signal to generalize to unseen but similar-looking merchant strings.
- The model is retrainable: every time a user corrects a prediction in the
  app, that correction is stored (backend/db) and folded back into the
  training set next time this script runs -> the model keeps improving
  on the user's own data over time (this is what makes it "their own AI"
  instead of a static rule table).

Run:
    python train_model.py

Produces:
    backend/ml/model.joblib       (trained classifier)
    backend/ml/vectorizer.joblib  (fitted TF-IDF vectorizer)
    backend/ml/labels.json        (sorted list of category names)
"""

import hashlib
import json
import random
import os
from pathlib import Path

import joblib
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import SGDClassifier
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR.parent / "data"
SEED_FILE = DATA_DIR / "category_seed.json"

MODEL_PATH = BASE_DIR / "model.joblib"
VECTORIZER_PATH = BASE_DIR / "vectorizer.joblib"
LABELS_PATH = BASE_DIR / "labels.json"
META_PATH = BASE_DIR / "meta.json"   # remembers which seed file this model was trained from

RANDOM = random.Random(42)


def _random_suffix():
    """Fake order-id / reference-number style noise banks append."""
    kind = RANDOM.choice(["digits", "alnum", "date"])
    if kind == "digits":
        return "".join(RANDOM.choice("0123456789") for _ in range(RANDOM.randint(4, 10)))
    if kind == "alnum":
        return "".join(RANDOM.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789") for _ in range(RANDOM.randint(4, 8)))
    return f"{RANDOM.randint(1,28):02d}{RANDOM.randint(1,12):02d}"


def build_synthetic_dataset(seed: dict, samples_per_category: int = 220):
    """
    Expand the small seed file (keywords + templates) into a larger
    synthetic labelled dataset by:
      1. Filling template placeholders with random noise (order IDs, dates)
      2. Randomly upper/lower/mixed-casing (real bank feeds are inconsistent)
      3. Occasionally concatenating a keyword directly with noise, the way
         real feeds often do (no spaces)
    This is only used to bootstrap the model before any real user data
    exists. It is not a substitute for real labelled data -- accuracy on
    a specific person's real statement will keep improving as corrections
    are logged and the model is retrained (see backend/ml/categorizer.py).
    """
    texts, labels = [], []
    for category, info in seed.items():
        keywords = info.get("keywords", [])
        templates = info.get("templates", [])
        for _ in range(samples_per_category):
            use_template = templates and RANDOM.random() < 0.7
            if use_template:
                tpl = RANDOM.choice(templates)
                text = tpl.replace("{n}", _random_suffix())
            else:
                kw = RANDOM.choice(keywords) if keywords else category
                text = f"{kw.upper()}{_random_suffix()}"

            # casing noise
            style = RANDOM.random()
            if style < 0.4:
                text = text.upper()
            elif style < 0.7:
                text = text.lower()
            # else: leave mixed case as-is

            texts.append(text)
            labels.append(category)
    return texts, labels


def load_corrections():
    """
    Pull any real user corrections logged by the running app
    (backend/db) so retraining improves on real feedback.
    Safe to skip if the DB / table doesn't exist yet.
    """
    try:
        import sys
        sys.path.append(str(BASE_DIR.parent))
        from db.database import SessionLocal
        from db.models import Correction

        session = SessionLocal()
        rows = session.query(Correction).all()
        texts = [r.description for r in rows]
        labels = [r.correct_category for r in rows]
        session.close()
        return texts, labels
    except Exception:
        return [], []


def main():
    with open(SEED_FILE) as f:
        seed = json.load(f)

    texts, labels = build_synthetic_dataset(seed)

    corr_texts, corr_labels = load_corrections()
    if corr_texts:
        # Real corrections are weighted more heavily by duplicating them,
        # so the model trusts actual user feedback over synthetic examples.
        texts += corr_texts * 5
        labels += corr_labels * 5
        print(f"Folded in {len(corr_texts)} user corrections (x5 weight).")

    X_train, X_test, y_train, y_test = train_test_split(
        texts, labels, test_size=0.15, random_state=42, stratify=labels
    )

    vectorizer = TfidfVectorizer(
        analyzer="char_wb",
        ngram_range=(3, 5),
        min_df=1,
        sublinear_tf=True,
    )
    X_train_vec = vectorizer.fit_transform(X_train)
    X_test_vec = vectorizer.transform(X_test)

    clf = SGDClassifier(
        loss="log_loss",       # gives predict_proba => confidence scores
        alpha=1e-5,
        max_iter=2000,
        random_state=42,
        class_weight="balanced",
    )
    clf.fit(X_train_vec, y_train)

    print("\n=== Held-out evaluation (on synthetic + any real corrections) ===")
    y_pred = clf.predict(X_test_vec)
    print(classification_report(y_test, y_pred, zero_division=0))

    joblib.dump(clf, MODEL_PATH)
    joblib.dump(vectorizer, VECTORIZER_PATH)
    with open(LABELS_PATH, "w") as f:
        json.dump(sorted(seed.keys()), f)
    with open(META_PATH, "w") as f:
        json.dump({"seed_sha": hashlib.sha256(SEED_FILE.read_bytes()).hexdigest()[:16]}, f)

    print(f"\nSaved model -> {MODEL_PATH}")
    print(f"Saved vectorizer -> {VECTORIZER_PATH}")
    print(f"Saved labels -> {LABELS_PATH}")


if __name__ == "__main__":
    main()
