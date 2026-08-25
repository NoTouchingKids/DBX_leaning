"""A small feed-forward classifier in PyTorch — the heavy-dependency model.

Its job in this platform is not classification accuracy. It is two things:

1. **Proving the microservice split.** Every other model here is light (numpy,
   emcee, gurobipy, or in one case nothing at all). This is the one whose
   dependency actually justifies per-model job environments: torch must not
   reach the models that do not want it, and only this job carries it.
2. **A third shape of training telemetry.** ``job/models/forecasting`` already
   reports one loss per epoch. This reports *two* levels — batch within epoch
   as well as epoch — with a bounded-and-meaningful ``primary_metric``
   (validation accuracy, 0..1) rather than an unbounded loss, and the torch
   **device** in the payload so a CPU run and a GPU run stay distinguishable
   after the fact.

Target, and the leakage that was avoided
----------------------------------------
The sample has exactly three usable columns: ``trip_distance``,
``fare_amount``, ``duration_min`` (``job/models/_data``, ``nyc_taxi_trips``). Any
two of them predict the third almost exactly on the real table, because a taxi
meter is a deterministic function of distance and time. So the obvious targets
are all traps:

* fare bucket from ``fare_amount`` (or from anything computed out of it) —
  the textbook leak, ~100% accuracy, meaningless.
* fare bucket from ``trip_distance`` + ``duration_min`` — no column is
  literally reused, but it is still the meter formula, so it is
  near-deterministic *on a workspace* while looking merely "easy" against the
  offline fallback. Rejected for the same reason.
* distance bucket from ``fare_amount`` — fare is ~2.6 * distance. Same trap.

What is left that is genuinely *predictable but not determined*:

    **target** — ``pace_class``: minutes per mile (``duration_min /
    trip_distance``), cut into ``fast`` / ``typical`` / ``congested`` at
    quantiles of the *training split only*.

    **feature** — ``trip_distance``, and nothing else, expanded into a small
    fixed basis (raw, ``log1p``, ``sqrt``, reciprocal). The basis is four
    numbers computed from one column; it adds representation, not information.

    **excluded** — ``duration_min``, because the target is derived from it;
    and ``fare_amount``, because the NYC meter charges for time as well as
    distance, so fare is a near-lossless proxy for duration. Feeding fare
    would make the target nearly deterministic on real data while looking
    harmless offline (the synthetic fallback's fare does not depend on
    duration at all) — a leak that only appears in production is the worst
    kind, so it is excluded on both paths. Both exclusions are written into
    every result row as ``excluded_features``, not just into this docstring.

``trip_distance`` appears in the target's denominator *and* in the features.
That is not leakage: the unobserved quantity is ``duration_min``, and knowing
the distance only tells you where the decision boundary sits in duration
space, not which side of it a trip falls. The numbers bear that out — roughly
0.67 validation accuracy against a 0.57 majority-class baseline, which is a
real but modest signal rather than a suspicious 0.99.

**Stated plainly, as the limitation it is:** three columns do not give a
non-leaking classification problem with more than one input feature. This is
the least-bad option, not a good one. If this model is ever pointed at a
richer table (pickup hour, zone, passenger count), the target can stay and the
feature set should grow.

Accuracy is also the *right* metric to report honestly but the wrong one to
report alone: the classes are deliberately imbalanced (~55/30/15), so the
payload and the result rows carry the majority-class baseline and a macro-F1
next to it. ``primary_metric`` stays accuracy because a generic progress view
needs one plottable, bounded number and "fraction correct" is the one a reader
who knows nothing about this model can interpret.

Cheap on purpose: a few thousand rows, two hidden layers, a dozen epochs,
about a second of CPU training. This is a platform test, not a benchmark.
"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Callable
from typing import Any

from .._data import Dataset, nyc_taxi_trips

__all__ = ["NeuralNetModel", "build_model", "CLASS_LABELS", "FEATURE_NAMES", "EXCLUDED_COLUMNS"]

#: Pace classes, in minutes per mile, low to high. Index order is the class
#: index the network predicts, so a reader can join the two without a lookup.
CLASS_LABELS = ("fast", "typical", "congested")

#: One column, four transforms of it. See the leakage note above.
FEATURE_NAMES = ("distance", "log1p_distance", "sqrt_distance", "inv_distance")

#: Deliberately withheld, and why — carried into the results table so the
#: decision survives longer than this file's docstring.
EXCLUDED_COLUMNS = {
    "duration_min": "the target is derived from it",
    "fare_amount": "the meter charges for time, so fare is a proxy for duration",
}


class NeuralNetModel:
    results_table = "results_neural_net"
    #: Three rows out, so a preview is the whole result set — but the axes
    #: still say which two columns a bar chart should lead with.
    preview_axes = ("class_index", "recall")

    def __init__(self, config: dict[str, Any] | None = None) -> None:
        cfg = dict(config or {})
        self.limit = int(cfg.get("limit", 4000))
        self.epochs = int(cfg.get("epochs", 12))
        self.batch_size = int(cfg.get("batch_size", 128))
        self.hidden = tuple(int(h) for h in cfg.get("hidden", (32, 16)))
        self.lr = float(cfg.get("lr", 0.01))
        #: Per-epoch multiplicative decay. Scheduled rather than constant so
        #: the learning rate in the payload is worth carrying.
        self.lr_decay = float(cfg.get("lr_decay", 0.9))
        self.val_fraction = float(cfg.get("val_fraction", 0.25))
        self.cut_quantiles = tuple(float(q) for q in cfg.get("cut_quantiles", (0.55, 0.85)))
        #: Intra-epoch progress samples. Kept small on purpose: per-batch
        #: emission would flood the live path for nothing.
        self.batch_updates = int(cfg.get("batch_updates_per_epoch", 2))
        self.seed = int(cfg.get("seed", 17))
        self.device_override = cfg.get("device")

        self.emit: Callable[..., Any] | None = None
        self.should_cancel: Callable[[], bool] | None = None

        self.data: Dataset | None = None
        self.data_meta: dict[str, Any] = {}
        self.device: Any = None
        self.history: list[dict[str, float]] = []
        self.best_accuracy: float | None = None
        self.cancelled = False
        self.cuts: tuple[float, float] | None = None
        self.train_time_seconds: float = 0.0

        self._net: Any = None
        self._best_state: dict[str, Any] | None = None
        self._tensors: dict[str, Any] = {}

    # --- setup ------------------------------------------------------------

    def _seed_everything(self) -> None:
        """Same seed, same result — a net that cannot be reproduced cannot be
        debugged.

        ``set_num_threads(1)`` is part of determinism, not just speed: the
        number of threads changes the order of float reductions, so a run on
        a 4-core laptop and a run on a 16-core node otherwise disagree in the
        last bits and then diverge through the optimiser.
        """
        import random

        import numpy as np
        import torch

        random.seed(self.seed)
        np.random.seed(self.seed)
        torch.manual_seed(self.seed)
        torch.cuda.manual_seed_all(self.seed)
        torch.set_num_threads(1)
        torch.backends.cudnn.deterministic = True
        torch.backends.cudnn.benchmark = False
        try:
            # warn_only: some ops have no deterministic kernel, and refusing
            # to run at all would be a worse outcome than a warning.
            torch.use_deterministic_algorithms(True, warn_only=True)
        except Exception as exc:  # noqa: BLE001 - never fail over a hint
            self._log(f"deterministic algorithms unavailable: {exc}", level="WARNING")

    def _pick_device(self) -> Any:
        """Detect, do not require. This is the model that would later want a
        GPU, so the device is discovered and then *reported* rather than
        assumed either way."""
        import torch

        if self.device_override:
            return torch.device(str(self.device_override))
        if torch.cuda.is_available():
            return torch.device("cuda")
        return torch.device("cpu")

    def _load(self) -> tuple[list[float], list[float]]:
        data = nyc_taxi_trips(limit=self.limit, seed=self.seed).dropna(
            "trip_distance", "duration_min"
        )
        self.data = data
        self.data_meta = data.describe()
        self._log(f"nyc taxi trips: {data.provenance}", phase="input")
        self._log(
            "target pace_class = duration_min / trip_distance, cut at training quantiles "
            f"{self.cut_quantiles}; features {FEATURE_NAMES} from trip_distance only; "
            "excluded " + ", ".join(f"{col} ({why})" for col, why in EXCLUDED_COLUMNS.items()),
            phase="input",
        )

        distance = [float(row["trip_distance"]) for row in data.rows]
        duration = [float(row["duration_min"]) for row in data.rows]
        usable = [(d, t) for d, t in zip(distance, duration, strict=True) if d > 0 and t > 0]
        dropped = len(distance) - len(usable)
        if dropped:
            self._log(
                f"dropped {dropped} rows with a non-positive distance or duration",
                phase="input",
                level="WARNING",
            )
        return [d for d, _ in usable], [t for _, t in usable]

    def build(self) -> None:
        """Load, label, split, standardise, and construct the network.

        Loading happens here rather than in ``__init__`` because the harness
        wires ``emit`` after construction — provenance logged from a
        constructor goes nowhere.
        """
        import numpy as np
        import torch
        from torch import nn

        self._seed_everything()
        self.device = self._pick_device()
        self._log(f"torch {torch.__version__} on device {self.device}", phase="build")

        distance, duration = self._load()
        if len(distance) < 200:
            raise ValueError(f"need at least 200 usable trips, got {len(distance)}")

        dist = np.asarray(distance, dtype=np.float64)
        pace = np.asarray(duration, dtype=np.float64) / dist

        # Shuffle before splitting: the real table arrives in pickup order,
        # and a positional split would be a time split dressed up as a random
        # one. Seeded, so it is the same shuffle every run.
        order = np.random.default_rng(self.seed).permutation(len(dist))
        dist, pace = dist[order], pace[order]
        split = int(len(dist) * (1.0 - self.val_fraction))

        # Cut points from the TRAIN split only. Quantiles taken over
        # everything would let the validation set decide its own labels.
        low, high = (float(q) for q in np.quantile(pace[:split], self.cut_quantiles))
        self.cuts = (low, high)
        labels = np.digitize(pace, [low, high]).astype(np.int64)

        features = np.stack([dist, np.log1p(dist), np.sqrt(dist), 1.0 / dist], axis=1)
        mean = features[:split].mean(axis=0)
        std = features[:split].std(axis=0)
        std[std == 0] = 1.0
        features = (features - mean) / std

        def tensor(array: Any, dtype: Any) -> Any:
            return torch.tensor(array, dtype=dtype, device=self.device)

        self._tensors = {
            "X_train": tensor(features[:split], torch.float32),
            "y_train": tensor(labels[:split], torch.long),
            "X_val": tensor(features[split:], torch.float32),
            "y_val": tensor(labels[split:], torch.long),
        }

        torch.manual_seed(self.seed)  # weight init, independent of the shuffle
        sizes = [len(FEATURE_NAMES), *self.hidden]
        layers: list[Any] = []
        for a, b in zip(sizes, sizes[1:], strict=False):
            layers += [nn.Linear(a, b), nn.ReLU()]
        layers.append(nn.Linear(sizes[-1], len(CLASS_LABELS)))
        self._net = nn.Sequential(*layers).to(self.device)

        counts = np.bincount(labels[:split], minlength=len(CLASS_LABELS)).tolist()
        self._log(
            f"{split} train / {len(dist) - split} val trips; pace cuts "
            f"{low:.2f}/{high:.2f} min per mile; train class counts "
            + ", ".join(f"{name}={n}" for name, n in zip(CLASS_LABELS, counts, strict=True)),
            phase="build",
        )

    # --- training ---------------------------------------------------------

    def run(self) -> str | None:
        import torch
        from torch import nn

        net, tensors = self._net, self._tensors
        X_train, y_train = tensors["X_train"], tensors["y_train"]
        n_train = int(X_train.shape[0])
        batches = max(1, math.ceil(n_train / self.batch_size))
        total_steps = batches * self.epochs
        checkpoints = self._checkpoint_batches(batches)

        optimiser = torch.optim.Adam(net.parameters(), lr=self.lr)
        schedule = torch.optim.lr_scheduler.ExponentialLR(optimiser, gamma=self.lr_decay)
        criterion = nn.CrossEntropyLoss()
        shuffler = torch.Generator(device="cpu").manual_seed(self.seed)

        started = time.monotonic()
        steps = 0
        for epoch in range(self.epochs):
            if self._cancelled():
                self._log(f"cancelled before epoch {epoch} of {self.epochs}")
                break

            net.train()
            order = torch.randperm(n_train, generator=shuffler).to(self.device)
            epoch_loss, grad_norm = 0.0, 0.0
            for batch in range(batches):
                index = order[batch * self.batch_size : (batch + 1) * self.batch_size]
                optimiser.zero_grad(set_to_none=True)
                loss = criterion(net(X_train[index]), y_train[index])
                loss.backward()
                grad_norm = float(
                    torch.sqrt(
                        sum(
                            (p.grad.detach() ** 2).sum()
                            for p in net.parameters()
                            if p.grad is not None
                        )
                    )
                )
                optimiser.step()
                epoch_loss += float(loss.detach()) * int(index.shape[0])
                steps += 1

                # Batch-level sample: the finer of the two progress levels,
                # throttled to a couple per epoch. Validation is re-evaluated
                # each time (it is ~1000 rows, so it costs nothing) rather
                # than repeating a stale number, so primary_metric is always
                # a live measurement.
                if batch in checkpoints and batch != batches - 1:
                    self._sample(
                        level="batch",
                        epoch=epoch,
                        batch=batch,
                        batches=batches,
                        train_loss=epoch_loss / min(n_train, (batch + 1) * self.batch_size),
                        grad_norm=grad_norm,
                        lr=float(optimiser.param_groups[0]["lr"]),
                        percent=100.0 * steps / total_steps,
                        elapsed=time.monotonic() - started,
                    )
                # Between batches as well as between epochs: cancellation
                # should not have to wait out an epoch on a bigger dataset.
                if self._cancelled():
                    break

            val_loss, accuracy, macro_f1 = self._evaluate()
            self.history.append(
                {
                    "epoch": epoch,
                    "train_loss": epoch_loss / n_train,
                    "val_loss": val_loss,
                    "val_accuracy": accuracy,
                    "macro_f1": macro_f1,
                }
            )
            # Best-checkpoint tracking, so a cancelled run keeps the best
            # weights it saw rather than whatever the last batch left behind.
            if self.best_accuracy is None or accuracy > self.best_accuracy:
                self.best_accuracy = accuracy
                self._best_state = {k: v.detach().clone() for k, v in net.state_dict().items()}

            self._sample(
                level="epoch",
                epoch=epoch,
                batch=batches - 1,
                batches=batches,
                train_loss=epoch_loss / n_train,
                grad_norm=grad_norm,
                lr=float(optimiser.param_groups[0]["lr"]),
                percent=100.0 * steps / total_steps,
                elapsed=time.monotonic() - started,
                val_loss=val_loss,
                accuracy=accuracy,
                macro_f1=macro_f1,
            )
            schedule.step()

            if self._cancelled():
                self._log(f"cancelled after epoch {epoch} of {self.epochs}")
                break

        self.train_time_seconds = time.monotonic() - started
        self._log(
            f"trained {len(self.history)} of {self.epochs} epochs in "
            f"{self.train_time_seconds:.2f}s on {self.device}; best val accuracy "
            f"{self.best_accuracy if self.best_accuracy is not None else float('nan'):.4f}",
            phase="run",
        )
        return "CANCELLED" if self.cancelled else None

    def _checkpoint_batches(self, batches: int) -> set[int]:
        """Which batch indices get an intra-epoch progress sample."""
        if self.batch_updates <= 0 or batches <= 1:
            return set()
        stride = max(1, batches // (self.batch_updates + 1))
        return {i * stride for i in range(1, self.batch_updates + 1) if i * stride < batches}

    def _cancelled(self) -> bool:
        if self.should_cancel is not None and self.should_cancel():
            self.cancelled = True
        return self.cancelled

    def _evaluate(self, state: dict[str, Any] | None = None) -> tuple[float, float, float]:
        import torch
        from torch import nn

        net = self._net
        if state is not None:
            net.load_state_dict(state)
        net.eval()
        with torch.no_grad():
            logits = net(self._tensors["X_val"])
            target = self._tensors["y_val"]
            loss = float(nn.functional.cross_entropy(logits, target))
            predicted = logits.argmax(dim=1)
            accuracy = float((predicted == target).float().mean())
        net.train()
        return loss, accuracy, self._macro_f1(predicted, target)

    @staticmethod
    def _macro_f1(predicted: Any, target: Any) -> float:
        f1s = []
        for c in range(len(CLASS_LABELS)):
            tp = float(((predicted == c) & (target == c)).sum())
            fp = float(((predicted == c) & (target != c)).sum())
            fn = float(((predicted != c) & (target == c)).sum())
            denominator = 2 * tp + fp + fn
            f1s.append(0.0 if denominator == 0 else 2 * tp / denominator)
        return sum(f1s) / len(f1s)

    # --- telemetry --------------------------------------------------------

    def _sample(
        self,
        *,
        level: str,
        epoch: int,
        batch: int,
        batches: int,
        train_loss: float,
        grad_norm: float,
        lr: float,
        percent: float,
        elapsed: float,
        val_loss: float | None = None,
        accuracy: float | None = None,
        macro_f1: float | None = None,
    ) -> None:
        """One progress message. Two levels, one shape.

        A generic view reads ``percent_complete`` and ``primary_metric`` and
        renders a chart with no idea what a taxi is. Everything a
        classifier-aware view would want later — the two-level counters, the
        gradient norm, the schedule, the device — rides in ``payload``.
        """
        if self.emit is None:
            return
        if accuracy is None:
            val_loss, accuracy, macro_f1 = self._evaluate()
        self.emit(
            "progress",
            elapsed_seconds=elapsed,
            percent_complete=min(100.0, percent),
            # Bounded 0..1 and plottable by anything. Accuracy alone flatters
            # an imbalanced problem, so the baseline it must beat travels
            # alongside it rather than being left for the reader to find.
            primary_metric=accuracy,
            primary_metric_label="val_accuracy",
            payload={
                "level": level,
                "epoch": epoch,
                "epochs_total": self.epochs,
                "batch": batch,
                "batches_per_epoch": batches,
                "train_loss": train_loss,
                "val_loss": val_loss,
                "macro_f1": macro_f1,
                "grad_norm": grad_norm,
                "learning_rate": lr,
                "best_val_accuracy": self.best_accuracy,
                "baseline_accuracy": self.baseline_accuracy(),
                # The one field that makes a CPU run and a GPU run
                # distinguishable after the fact.
                "device": str(self.device),
                "data_synthetic": self.data_meta.get("data_synthetic"),
            },
        )

    # --- results ----------------------------------------------------------

    def baseline_accuracy(self) -> float:
        """What predicting the majority class alone would score on validation.

        On imbalanced classes a headline accuracy hides an expensive constant
        function; this is the number that exposes it, so it is reported next
        to the accuracy everywhere the accuracy appears.
        """
        target = self._tensors.get("y_val")
        if target is None or int(target.shape[0]) == 0:
            return 0.0
        counts = [int((target == c).sum()) for c in range(len(CLASS_LABELS))]
        return max(counts) / int(target.shape[0])

    def results(self) -> list[dict[str, Any]]:
        """One row per class: precision, recall, F1, support, confusion row —
        plus the run-level metrics repeated on each, so the table answers
        "was this better than a constant?" without a join.

        Evaluated from the best checkpoint, which is what a cancelled run has
        instead of a finished one.
        """
        import torch

        if self._net is None or self._best_state is None:
            return []

        self._net.load_state_dict(self._best_state)
        self._net.eval()
        with torch.no_grad():
            logits = self._net(self._tensors["X_val"])
            predicted = logits.argmax(dim=1)
        target = self._tensors["y_val"]
        total = int(target.shape[0])
        val_loss, accuracy, macro_f1 = self._evaluate(self._best_state)
        baseline = self.baseline_accuracy()
        low, high = self.cuts or (float("nan"), float("nan"))

        recalls: list[float] = []
        rows: list[dict[str, Any]] = []
        for index, label in enumerate(CLASS_LABELS):
            tp = int(((predicted == index) & (target == index)).sum())
            fp = int(((predicted == index) & (target != index)).sum())
            fn = int(((predicted != index) & (target == index)).sum())
            support = tp + fn
            precision = tp / (tp + fp) if tp + fp else 0.0
            recall = tp / support if support else 0.0
            f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
            recalls.append(recall)
            confusion = {
                name: int(((target == index) & (predicted == other)).sum())
                for other, name in enumerate(CLASS_LABELS)
            }
            rows.append(
                {
                    "class_index": index,
                    "class_label": label,
                    "precision": round(precision, 6),
                    "recall": round(recall, 6),
                    "f1": round(f1, 6),
                    "support": support,
                    "true_positives": tp,
                    "false_positives": fp,
                    "false_negatives": fn,
                    # The full row of the confusion matrix as JSON — VARIANT
                    # is nice-to-have, a JSON string is the portable floor
                    # (CLAUDE.md), and tp/fp/fn above stay queryable as ints.
                    "confusion_row": json.dumps(confusion),
                }
            )

        balanced = sum(recalls) / len(recalls)
        overall = {
            "accuracy": round(accuracy, 6),
            "macro_f1": round(macro_f1, 6),
            "balanced_accuracy": round(balanced, 6),
            # Beat this or the network is an expensive constant function.
            "baseline_accuracy": round(baseline, 6),
            "lift_over_baseline": round(accuracy - baseline, 6),
            "val_loss": round(val_loss, 6),
            "val_rows": total,
            "train_rows": int(self._tensors["X_train"].shape[0]),
            "epochs_trained": len(self.history),
            "epochs_planned": self.epochs,
            "cancelled": self.cancelled,
            "seed": self.seed,
            "device": str(self.device),
            "torch_version": torch.__version__,
            "train_time_seconds": round(self.train_time_seconds, 6),
            "target": "pace_class",
            "pace_cut_low": round(low, 6),
            "pace_cut_high": round(high, 6),
            "features": ",".join(FEATURE_NAMES),
            # The leakage decision, in the durable record rather than only in
            # a docstring nobody reads six months later.
            "excluded_features": ",".join(EXCLUDED_COLUMNS),
            **self.data_meta,
        }
        return [{**row, **overall} for row in rows]

    # --- plumbing ---------------------------------------------------------

    def _log(self, message: str, *, phase: str = "run", level: str = "INFO") -> None:
        if self.emit is not None:
            self.emit("log", message=message, level=level, source="model", phase=phase)


def build_model(config: dict[str, Any] | None = None) -> NeuralNetModel:
    return NeuralNetModel(config)
