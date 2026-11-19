"""

Trains the spatial layer that combines fixed KRONOS image features before RF/SVM fitting.

We start with each image patch's encoder vector and the mean encoder vector
of its connected neighbours. A learned linear combination of these two
inputs, followed by ReLU, produces H spatial features. A temporary linear
classification layer maps those features to the reference classes, providing
the training error used to learn the spatial weights.

After fitting, we retain the spatial weights and calculate the H-feature
rows used by the final RF/SVM. We also save the temporary classification
layer and epoch-loss history for inspection of the spatial-model fit.
"""

# ==============================================================================
#                                    Imports
# ==============================================================================

from __future__ import annotations
from dataclasses import dataclass
from collections.abc import Mapping, Callable
import hashlib
import json
import math
import numpy as np
from .patch_graph import PatchGraph, GraphInputError, GraphBudgetError
from .graph_features import FrozenMeanSelf

# ==============================================================================
#                                 Graph training
# ==============================================================================


class GraphTrainingCancelled(RuntimeError):
    """

    Reports that graph fitting was cancelled before a completed model was returned.
    """

@dataclass(frozen=True, slots=True)
class GraphTrainingSettings:
    """

    Defines the learned feature dimension and the settings used to fit the spatial layer.

    We select H output features per patch, the number of passes through the
    labelled training patches and the number of patches per parameter update.
    Increasing H adds learned weights and increases the input columns supplied
    to RF/SVM. Image positions and connections are supplied by the existing patch graph.

    Attributes:
        hidden_width: Number H of learned features per patch, from one to 768.
        epochs: Complete passes through labelled patches, from one to 1000.
        batch_size: Labelled patches used for one update, from one to 4096.
        learning_rate: Positive Adam learning rate no greater than one.
        weight_decay: Adam weight-decay setting in [0,1].
        random_seed: Seed for model initialization and training-row shuffling.
        max_working_bytes: Allowance compared with the estimate for training
            input matrices, learned parameters, optimizer state and batch arrays.
    """

    hidden_width: int
    epochs: int
    batch_size: int
    learning_rate: float
    weight_decay: float
    random_seed: int
    max_working_bytes: int

    def __post_init__(self):
        """

        Checks the feature dimension, training counts, optimizer settings and memory limit.

        We require positive integer dimensions and counts within their supported
        limits. The learning rate must be finite and positive, weight decay finite
        and nonnegative, and the random seed within the supported 31-bit interval.

        Raises:
            ValueError: A setting has the wrong type or falls outside its interval.
        """

        for name, maximum in (("hidden_width", 768), ("epochs", 1000), ("batch_size", 4096)):
            value = getattr(self, name)
            if type(value) is not int or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}")
        if type(self.random_seed) is not int or not 0 <= self.random_seed < 2**31:
            raise ValueError("random_seed must be a nonnegative 31-bit integer")
        if type(self.max_working_bytes) is not int or self.max_working_bytes <= 0:
            raise ValueError("max_working_bytes must be positive")
        if (
            isinstance(self.learning_rate, bool)
            or not math.isfinite(self.learning_rate)
            or not 0 < self.learning_rate <= 1
        ):
            raise ValueError("learning_rate must be finite and in (0,1]")
        if (
            isinstance(self.weight_decay, bool)
            or not math.isfinite(self.weight_decay)
            or not 0 <= self.weight_decay <= 1
        ):
            raise ValueError("weight_decay must be finite and in [0,1]")


@dataclass(frozen=True, slots=True)
class LearnedGraphFeatures:
    """

    Retains the trained spatial layer, temporary classification layer and references used for fitting.

    The spatial parameters determine the features supplied to RF/SVM. The
    temporary class layer and per-epoch loss values allow us to review how that
    representation was fitted. Ordered patch/class assignments identify the
    exact labelled references used by this fit.

    Attributes:
        parameters: Trained self/neighbour matrices and spatial bias.
        training_node_ids: Labelled patch IDs in the training-array order.
        training_class_ids: Class order used for integer labels and class-score columns.
        loss_history: Mean training loss encountered during each epoch, weighted
            by the number of patches in each batch.
        settings: Resolved graph-fitting settings.
        head_weight: Temporary class-layer matrix of shape (H, number_of_classes).
        head_bias: One temporary class-layer bias per class.
        label_assignments_sha256: Checksum of ordered labelled patch IDs and classes.
    """

    parameters: FrozenMeanSelf
    training_node_ids: tuple[str, ...]
    training_class_ids: tuple[str, ...]
    loss_history: tuple[float, ...]
    settings: GraphTrainingSettings
    head_weight: np.ndarray
    head_bias: np.ndarray
    label_assignments_sha256: str

    def __post_init__(self):
        """

        Checks the temporary class layer against the fitted feature and class dimensions.

        The weight matrix must map H spatial features to the declared classes and
        its bias must have one entry per class. We copy these finite float32 values
        into read-only arrays before retaining them for model review.

        Raises:
            GraphInputError: The class-layer shape, dtype or numerical values are invalid.
        """

        for name, shape in (
            ("head_weight", (self.settings.hidden_width, len(self.training_class_ids))),
            ("head_bias", (len(self.training_class_ids),)),
        ):
            value = getattr(self, name)
            if (
                not isinstance(value, np.ndarray)
                or value.dtype != np.float32
                or value.shape != shape
                or not np.isfinite(value).all()
            ):
                raise GraphInputError(
                    "provisional head must have finite float32 parameters of the declared shape"
                )
            object.__setattr__(
                self, name, np.frombuffer(value.tobytes(), dtype=np.float32).reshape(shape)
            )


def _training_arrays(graph, features, labels, classes, settings):
    """

    Prepares the fixed self/mean-neighbour feature matrices used in every graph-training epoch.

    We require training-role nodes from one fold, a feature vector for every
    graph node and labels for a subset that represents every declared class.
    Unlabelled training-image patches may provide neighbour features. Only
    labelled patches receive rows in the supervised training arrays.

    For L labelled patches and D encoder features, we allocate own and mean
    matrices of shape (L,D) and an integer target vector of length L. Each own
    row receives that patch's encoder vector. Its mean row contains the average
    of the available left/right/above/below vectors, or zeros for an isolated
    patch. The target integer is the label's position in classes.

    Before allocation, we compare max_working_bytes with the current estimate:

        4 * (2*L*D + 12*(2*D*H + H*K) + B*(2*D + 4*H + 2*K))

    Here H is the learned feature dimension, K is the class count and B the
    selected batch size. The terms allow the two labelled input matrices,
    model/optimizer-related arrays and temporary batch arrays. Input features,
    Python objects and framework memory occupy additional process memory.

    Args:
        graph: PatchGraph assigned to this training fold.
        features: Finite float32 vectors indexed by every graph node ID.
        labels: Class names indexed by the labelled node subset.
        classes: Unique output class names in their model order.
        settings: Selected dimension, batch size and numerical-array allowance.

    Returns:
        Labelled node IDs, own-feature matrix, neighbour-mean matrix and integer
        target vector, all in the graph's corresponding labelled-row order.

    Raises:
        GraphInputError: Node roles, fold IDs, feature types/widths or class
            coverage are incompatible, or aggregation becomes nonfinite.
        GraphBudgetError: Estimated numerical arrays exceed the selected allowance.
    """

    if type(graph) is not PatchGraph or type(settings) is not GraphTrainingSettings:
        raise GraphInputError("training requires a bounded patch graph and explicit settings")
    if any(node.scope.split != "training" for node in graph.nodes):
        raise GraphInputError("Only training split nodes may enter graph training")
    if len({node.scope.fold_id for node in graph.nodes}) != 1:
        raise GraphInputError("A graph fit must belong to one training fold")
    if (
        type(classes) is not tuple
        or not 2 <= len(classes) <= 128
        or len(set(classes)) != len(classes)
    ):
        raise GraphInputError("training class order must contain 2–128 distinct class names")
    if any(type(c) is not str or not c.strip() for c in classes):
        raise GraphInputError("training class names must be nonempty text")
    if not isinstance(features, Mapping) or not isinstance(labels, Mapping):
        raise GraphInputError("features and labels must be explicit node mappings")
    node_ids = {node.node_id for node in graph.nodes}
    if set(features) != node_ids:
        raise GraphInputError("feature inventory must exactly cover the training graph")
    if not labels or not set(labels) <= node_ids or set(labels.values()) != set(classes):
        raise GraphInputError(
            "labelled nodes must be in the graph and represent every declared class"
        )
    first = features[graph.nodes[0].node_id]
    if (
        not isinstance(first, np.ndarray)
        or first.dtype != np.float32
        or first.ndim != 1
        or not 1 <= first.size <= 4096
    ):
        raise GraphInputError("features must be finite float32 vectors of one declared width")
    width = first.size
    for value in features.values():
        if (
            not isinstance(value, np.ndarray)
            or value.dtype != np.float32
            or value.shape != (width,)
            or not np.all(np.isfinite(value))
        ):
            raise GraphInputError(
                "all graph features must be finite float32 vectors of the same width"
            )
    ordered = tuple(node.node_id for node in graph.nodes if node.node_id in labels)
    # Estimate both labelled input matrices, trainable parameters, optimizer
    # state and batch calculations before allocating the training arrays.
    estimate = 4 * (
        2 * len(ordered) * width
        + 12 * (2 * width * settings.hidden_width + settings.hidden_width * len(classes))
        + settings.batch_size * (2 * width + 4 * settings.hidden_width + 2 * len(classes))
    )
    if estimate > settings.max_working_bytes:
        raise GraphBudgetError("graph training matrices exceed the explicit working-array budget")
    own = np.empty((len(ordered), width), np.float32)
    mean = np.zeros_like(own)
    target = np.empty(len(ordered), np.int64)
    for i, node_id in enumerate(ordered):
        own[i] = features[node_id]
        neighbours = graph.neighbors(node_id)
        for neighbour in neighbours:
            mean[i] += features[neighbour]
        if neighbours:
            mean[i] /= len(neighbours)
        target[i] = classes.index(labels[node_id])
    if not np.all(np.isfinite(mean)):
        raise GraphInputError("neighbour feature aggregation is nonfinite")
    return ordered, own, mean, target


def train_mean_self(
    graph: PatchGraph,
    features: Mapping[str, np.ndarray],
    labels: Mapping[str, str],
    class_order: tuple[str, ...],
    settings: GraphTrainingSettings,
    *,
    cancelled: Callable[[], bool] | None = None,
    on_epoch: Callable[[int, float], None] | None = None,
) -> LearnedGraphFeatures:
    """

    Fits one spatial-feature layer from labelled patches and fixed encoder vectors.

    Let e[i] be patch i's encoder vector and m[i] the mean of its available
    neighbours. We calculate H spatial features and temporary class scores as:

        h[i] = ReLU(e[i] @ W_self + m[i] @ W_neighbour + b)
        z[i] = h[i] @ W_head + b_head
        p[i,k] = exp(z[i,k]) / sum_j(exp(z[i,j]))

    ReLU sets negative combined features to zero. The softmax expression
    converts class scores z into class probabilities p. For reference class
    y[i], the mean cross-entropy loss is -mean_i(log(p[i,y[i]])). Assigning a
    small probability to the reference class therefore increases the loss.
    PyTorch calculates this loss directly from the class scores in a numerically
    stable form.

    i) Prepare the labelled self/neighbour matrices, select the processing
    device and initialize the spatial matrices and temporary class layer using
    the recorded random seed. The self layer carries the spatial bias; the
    neighbour layer contributes only its matrix product.

    ii) Shuffle labelled rows for each epoch and process them in batches. For
    each batch, calculate the class loss and backpropagate its gradients through
    the temporary class layer and the spatial layer. Adam uses running estimates
    of the gradients and their squared magnitudes to update these parameters
    with the selected learning rate and weight decay. Encoder vectors stay fixed.

    iii) Repeat for the configured epoch count, checking cancellation before
    each epoch and batch. Record the batch-size-weighted mean loss encountered
    during each epoch. Held-out performance is evaluated by the surrounding
    subject-split workflow after this fit.

    iv) Retain the trained spatial matrices and temporary class-layer parameters.
    We transpose PyTorch Linear's (outputs, inputs) weights to the (inputs,
    outputs) convention used by row-vector prediction. The subsequent native
    RF/SVM fit uses h[i] from this fixed spatial representation.

    Args:
        graph: PatchGraph containing the permitted nodes for this training fold.
        features: Finite float32 encoder vectors indexed by graph node ID.
        labels: Reference class assignments for the labelled patches.
        class_order: Distinct classes defining integer labels and class-score columns.
        settings: Feature dimension, epochs, batch size, optimizer and memory settings.
        cancelled: Optional callback returning True when fitting should stop.
        on_epoch: Optional callback receiving the completed epoch count and mean loss.

    Returns:
        LearnedGraphFeatures containing the completed spatial model, temporary
        class layer, training-reference assignments and loss history.

    Raises:
        GraphInputError: Inputs are invalid or a calculated training loss is nonfinite.
        GraphBudgetError: The required training arrays exceed their memory limit.
        GraphTrainingCancelled: Cancellation is requested before the fit completes.
    """

    def check_cancelled():
        """

        Stops fitting when the supplied cancellation callback requests it.

        We check between epochs and batches so cancellation is handled before
        another parameter update starts.

        Raises:
            GraphTrainingCancelled: The callback returns True.
        """

        if cancelled is not None and cancelled():
            raise GraphTrainingCancelled("Spatial-model training was cancelled")

    check_cancelled()
    ids, own, neighbours, labels_array = _training_arrays(
        graph, features, labels, class_order, settings
    )
    # Prepare PyTorch only after the selected training inputs and allocation
    # have been checked. The frozen encoder features are already available.
    import torch
    from mif_pipeline._devices import select_torch_device

    device = select_torch_device(
        "gnn", torch=torch, required_bytes=settings.max_working_bytes
    ).device
    import sys

    print("GNN training device: " + device, file=sys.stderr, flush=True)
    cuda_devices = [torch.cuda.current_device()] if device == "cuda" else []
    with torch.random.fork_rng(devices=cuda_devices):
        torch.manual_seed(settings.random_seed)
        own_tensor = torch.from_numpy(own).to(device)
        neighbour_tensor = torch.from_numpy(neighbours).to(device)
        targets = torch.from_numpy(labels_array).to(device)
        self_layer = torch.nn.Linear(own.shape[1], settings.hidden_width, bias=True)
        neighbour_layer = torch.nn.Linear(own.shape[1], settings.hidden_width, bias=False)
        head = torch.nn.Linear(settings.hidden_width, len(class_order))
        self_layer.to(device)
        neighbour_layer.to(device)
        head.to(device)
        parameters = (
            list(self_layer.parameters())
            + list(neighbour_layer.parameters())
            + list(head.parameters())
        )
        optimizer = torch.optim.Adam(
            parameters, lr=settings.learning_rate, weight_decay=settings.weight_decay
        )
        history = []
        for epoch in range(settings.epochs):
            check_cancelled()
            order = torch.randperm(len(ids))
            epoch_loss = 0.0
            for begin in range(0, len(ids), settings.batch_size):
                check_cancelled()
                selected = order[begin : begin + settings.batch_size].to(device)
                optimizer.zero_grad(set_to_none=True)
                # Learn how much each patch's own features and neighbouring
                # features contribute before the temporary class prediction.
                hidden = torch.relu(
                    self_layer(own_tensor[selected]) + neighbour_layer(neighbour_tensor[selected])
                )
                loss = torch.nn.functional.cross_entropy(head(hidden), targets[selected])
                if not bool(loss.isfinite()):
                    raise GraphInputError("graph training loss became nonfinite")
                # Backpropagate into the graph layer and temporary head;
                # the precomputed encoder vectors remain fixed.
                loss.backward()
                optimizer.step()
                epoch_loss += float(loss.detach()) * len(selected)
            history.append(epoch_loss / len(ids))
            if on_epoch is not None:
                on_epoch(epoch + 1, history[-1])
        check_cancelled()
        # Store matrices for row-vector inference: PyTorch Linear keeps
        # weights as (outputs, inputs), so transpose them to (inputs, outputs).
        frozen = FrozenMeanSelf(
            self_layer.weight.detach().cpu().numpy().T.copy(),
            neighbour_layer.weight.detach().cpu().numpy().T.copy(),
            self_layer.bias.detach().cpu().numpy().copy(),
        )
        head_weight = head.weight.detach().cpu().numpy().T.copy()
        head_bias = head.bias.detach().cpu().numpy().copy()
    assignment_hash = hashlib.sha256(
        json.dumps(
            [(name, labels[name]) for name in ids], ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return LearnedGraphFeatures(
        frozen, ids, class_order, tuple(history), settings, head_weight, head_bias, assignment_hash
    )


