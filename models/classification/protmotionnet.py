import os
from typing import final, Callable, Type, Optional, Union, Any
import einops
import torch
from torch import Tensor
import torch.nn.functional as F
from torch.nn import MultiheadAttention, TransformerEncoderLayer, TransformerEncoder
from torch.utils.tensorboard import SummaryWriter
from torch_geometric.data import Data
from torch_geometric.loader import DataLoader
from torch_geometric.nn.aggr import LSTMAggregation, SoftmaxAggregation, MaxAggregation, MeanAggregation, SumAggregation
from torch_geometric.nn.dense import Linear  # , dense_diff_pool this has to be in the "future work"
from torch_geometric.utils import to_dense_batch
from log.logger import Logger
from models.batch_utils import generate_batch_cross_attention_mask, from_dense_batch
from models.classification.classifiers import GraphClassifier, MulticlassClassificationLoss, ClassificationLoss
from models.layers import SerializableModule
from functools import partial
from preprocessing.dataset.paired_dataset import PairedDataLoader
from training.training_tools import EARLY_STOP_PATIENCE, EarlyStopping, MetricsHistoryTracer, FIGURE_SIZE_DEFAULT


CLASSES: final = 7


class ProtMotionNet(GraphClassifier):
    __READOUTS: final = {
        "mean_pool": MeanAggregation(),
        "max_pool": MaxAggregation(),
        "add_pool": SumAggregation(),
        'lstm': LSTMAggregation,
        'softmax': SoftmaxAggregation(learn=True)
    }
    READOUTS: final = frozenset(__READOUTS.keys())

    _ACTIVATIONS: final = {
        "linear": torch.nn.Identity(),
        "relu": F.relu,
        "leaky_relu": F.leaky_relu,
        "rrelu": F.rrelu,
        "relu6": F.relu6,
        "gelu": partial(F.gelu, approximate='none'),
        "elu": F.elu,
        "celu": F.celu,
        "glu": F.glu,
        "selu": F.selu,
        "prelu": F.prelu,
        "silu": F.silu,
        "hardswish": F.hardswish,
        "tanh": F.tanh,
        "sigmoid": torch.sigmoid,
        "softmax": partial(F.softmax, dim=-1)
    }
    ACTIVATIONS: final = frozenset(_ACTIVATIONS.keys())

    def __init__(self, encoder: SerializableModule, encoder_out_channels: int, dense_units: list[int],
                 dense_activations: list[str], dim_features: int, dropout: float = 0.0, readout: str = 'mean_pool'):

        super(ProtMotionNet, self).__init__(dim_features=dim_features,
                                            dim_target=dense_units[-1],
                                            config={"readout": readout, "dense_units": dense_units,
                                                    "dense_activations": dense_activations, "dropout": dropout}
                                            )

        # Validate input
        if len(dense_units) != len(dense_activations):
            raise ValueError(f"len(dense_activations) must be equal to len(dense_units),"
                             f" {len(dense_activations)} and {len(dense_units)} given")

        self._encoder = encoder
        self.__encoder_out_channels = encoder_out_channels
        self._readout_aggregation = self.__resolve_readout(readout, encoder_out_channels)
        # self.__dense_units = dense_units
        # self.__dense_activations = dense_activations
        # self.__dropout = dropout
        # self.__readout = readout

        # Build dense layers
        self._dense_layers = torch.nn.ModuleList()
        prev_units = encoder_out_channels
        for i in range(0, len(dense_units)):

            # Check if activations
            if dense_activations[i] not in self._ACTIVATIONS:
                raise ValueError(f"Activation function must be one of {self.ACTIVATIONS}, {dense_activations[i]} given")

            # Build dense layer
            self._dense_layers.append(Linear(prev_units, dense_units[i]))
            prev_units = dense_units[i]

    def _apply_dense_layers(self, x: Tensor) -> Tensor:

        for i in range(0, len(self._dense_layers)):

            # Apply dense layer and activation
            dense = self._dense_layers[i]
            activation = self._ACTIVATIONS[self.dense_activations[i]]
            x = dense(x)
            x = activation(x)

            # Apply dropout on the activation, except if it is the output one
            if i < len(self._dense_layers) - 1:
                x = F.dropout(x, p=self.dropout, training=self.training)

        return x

    def forward(self, x, edge_index, batch_index: Tensor = None, *args, **kwargs):

        # Extract features with encoder
        x = self._encoder(x, edge_index, *args, **kwargs)

        # Apply readout aggregation, assuming batch is a single graph if batch_index is not given
        if batch_index is None:
            batch_index = torch.zeros(size=(x.shape[-2],)).type(torch.int64)
        x = self._readout_aggregation(x, index=batch_index)

        x = self._apply_dense_layers(x)

        return x

    @property
    def dropout(self) -> float:
        return self.config_dict["dropout"]

    @property
    def dense_units(self) -> list[int]:
        return self.config_dict["dense_units"]

    @property
    def dense_activations(self) -> list[str]:
        return self.config_dict["dense_activations"]

    @property
    def readout(self) -> str:
        return self.config_dict["readout"]

    @property
    def encoder_out_channels(self) -> int:
        return self.__encoder_out_channels

    def serialize_constructor_params(self, *args, **kwargs) -> dict:

        # Serialize constructor parameters
        constructor_params = {
            "encoder_out_channels": self.encoder_out_channels,
            "dense_units": self.dense_units,
            "dense_activations": self.dense_activations,
            "dropout": self.dropout,
            "readout": self.readout
        }

        # Serialize encoder
        constructor_params["encoder"] = {
            "constructor_params": self._encoder.serialize_constructor_params(),
            "state_dict": self._encoder.state_dict()
        }

        return constructor_params

    # noinspection PyMethodOverriding
    @classmethod
    def from_constructor_params(cls,
                                constructor_params: dict,
                                encoder_constructor: Type[SerializableModule],
                                *args, **kwargs):
        # Deserialize encoder
        encoder_constructor_params = constructor_params["encoder"]["constructor_params"]
        encoder_state_dict = constructor_params["encoder"]["state_dict"]
        # TODO: Could not work if from_constructor_params is overridden by encoder class, must fix this to generalize
        encoder = encoder_constructor.from_constructor_params(encoder_constructor_params)
        encoder.load_state_dict(encoder_state_dict)
        del constructor_params["encoder"]  # delete encoder params from constructor_params

        return cls(encoder=encoder, **constructor_params)

    @classmethod
    def __resolve_readout(cls, readout: str, channels: int) -> Callable:
        if readout not in cls.READOUTS:
            raise ValueError(f"Readout function must be in {cls.READOUTS}")

        if readout == 'lstm':
            return LSTMAggregation(in_channels=channels, out_channels=channels)
        return cls.__READOUTS[readout]


class PairedProtMotionNet(ProtMotionNet):
    def __init__(self, encoder: SerializableModule, encoder_out_channels: int, dense_units: list[int],
                 dense_activations: list[str], dim_features: int, num_heads: int = 8, kdim: Optional[int] = None,
                 vdim: Optional[int] = None, dropout: float = 0.0, readout: str = 'mean_pool'):
        super(PairedProtMotionNet, self).__init__(encoder=encoder, encoder_out_channels=encoder_out_channels,
                                                  dense_units=dense_units, dense_activations=dense_activations,
                                                  dim_features=dim_features, dropout=dropout, readout=readout)
        self._multi_head_attention = MultiheadAttention(embed_dim=encoder_out_channels, num_heads=num_heads,
                                                        dropout=dropout, kdim=kdim, vdim=vdim, batch_first=False)
        self.__vdim: Optional[int] = vdim
        self.__kdim: Optional[int] = kdim
        self.__num_heads: int = num_heads

    @property
    def vdim(self) -> int:
        return self.__vdim if self.__vdim is not None else self.encoder_out_channels

    @property
    def kdim(self) -> int:
        return self.__kdim if self.__kdim is not None else self.encoder_out_channels

    @property
    def num_heads(self) -> int:
        return self.__num_heads

    def serialize_constructor_params(self, *args, **kwargs) -> dict:
        constructor_params = super(PairedProtMotionNet, self).serialize_constructor_params(*args, **kwargs)
        constructor_params.update({"num_heads": self.__num_heads, "vdim": self.__vdim, "kdim": self.__kdim})
        return constructor_params

    def _get_multi_head_attention_embeddings(self,
                                             x: Union[Tensor, tuple[Tensor, Tensor]],
                                             edge_index: Union[Tensor, tuple[Tensor, Tensor]],
                                             batch_index: Optional[Union[Tensor, tuple[Tensor, Tensor]]] = None,
                                             x1: Optional[Tensor] = None,
                                             edge_index1: Optional[Tensor] = None,
                                             batch_index1: Optional[Tensor] = None,
                                             *args, **kwargs):

        # Check input
        if isinstance(x, tuple) and len(x) < 2:
            raise ValueError(f"Exactly 2 x tensors must be given to the {self.__class__} forward() method. {len(x)} "
                             f"given.")
        elif not isinstance(x, tuple) and x1 is None:
            raise ValueError(f"Exactly 2 x tensors must be given to the {self.__class__} forward() method. Just 1 "
                             f"given.")

        if isinstance(edge_index, tuple) and len(edge_index) < 2:
            raise ValueError(f"Exactly 2 edge indexes must be given to the {self.__class__} forward() method. "
                             f"{len(edge_index)} given.")
        elif not isinstance(edge_index, tuple) and edge_index1 is None:
            raise ValueError(f"Exactly 2 edge index tensors must be given to the {self.__class__} forward() method. "
                             f"Just 1 given.")
        if batch_index is not None and isinstance(batch_index, tuple) and len(batch_index) < 2:
            raise ValueError(f"Exactly 2 batch index tensor must be given to the {self.__class__} forward() method. "
                             f"{len(batch_index)} given.")
        elif batch_index is not None and not isinstance(batch_index, tuple) and batch_index1 is None:
            raise ValueError(f"Exactly 2 batch index tensors must be given to the {self.__class__} forward() method. "
                             f"Just 1 given.")

        # Setup the two graphs
        if isinstance(x, tuple):
            x1: Tensor = x[1]
            x: Tensor = x[0]
        if isinstance(edge_index, tuple):
            edge_index1: Tensor = edge_index[1]
            edge_index: Tensor = edge_index[0]
        if batch_index is not None and isinstance(batch_index, tuple):
            batch_index1: Tensor = batch_index[1]
            batch_index: Tensor = batch_index[0]

        # Assume batch is a single graph if batch_index is not given
        elif batch_index is None:
            batch_index = torch.zeros(size=(x.shape[-2],)).type(torch.int64)
            batch_index1 = torch.zeros(size=(x1.shape[-2],)).type(torch.int64)

        # Extract features with encoder
        x = self._encoder(x, edge_index, *args, **kwargs)
        x1 = self._encoder(x1, edge_index1, *args, **kwargs)

        # Convert to dense batch
        # TODO: this doesn't work as intended, must pass explicitly the max_nodes parameter
        x, mask0 = to_dense_batch(x=x, batch=batch_index, max_num_nodes=None, fill_value=0)
        x1, mask1 = to_dense_batch(x=x1, batch=batch_index1, max_num_nodes=None, fill_value=0)

        # Change tensor shape to match the transformer [seq_length, batch_size, dim]
        x = einops.rearrange(x, "b s f -> s b f")
        x1 = einops.rearrange(x1, "b s f -> s b f")

        # Generate key padding mask and attention mask
        attn_mask = generate_batch_cross_attention_mask(batch_padding_mask_query=mask1,
                                                        batch_padding_mask_key=mask0,
                                                        num_heads=self.num_heads)

        # Key padding mask must be created according to the query sequence since the output shape of cross attention
        # from it, but invert True with False and False with True since True positions are not allowed to attend
        key_padding_mask = mask0 == False

        # Apply cross multi-head attention
        # TODO: check the longer between x and x1, so that the longer is used as query (using x1 should be fine however)
        x, _ = self._multi_head_attention(query=x1, key=x, value=x, key_padding_mask=None,
                                          need_weights=False, attn_mask=attn_mask)

        # Add shortcut-connection with concat/add with the query
        x = x + x1

        # Free memory
        del x1  # no need to further memorize this
        del _
        torch.cuda.empty_cache()

        return x, key_padding_mask, mask1

    def forward(self,
                x: Union[Tensor, tuple[Tensor, Tensor]],
                edge_index: Union[Tensor, tuple[Tensor, Tensor]],
                batch_index: Optional[Tensor] = None,
                x1: Optional[Tensor] = None,
                edge_index1: Optional[Tensor] = None,
                batch_index1: Optional[Tensor] = None,
                *args, **kwargs) -> torch.Tensor:

        # Get cross multi-head attention embeddings
        x, _, batch_mask = self._get_multi_head_attention_embeddings(
            x=x,
            edge_index=edge_index,
            batch_index=batch_index,
            x1=x1,
            edge_index1=edge_index1,
            batch_index1=batch_index1,
            *args,
            **kwargs
        )

        del _
        torch.cuda.empty_cache()

        # Convert from (sequence_length, batch_size, embedding_size) to (batch_size, sequence_length, embedding_size)
        x = einops.rearrange(x, "s b f -> b s f")

        # Convert to PyG batch format again
        x, _ = from_dense_batch(dense_batch=x, mask=batch_mask)

        del _, batch_mask
        torch.cuda.empty_cache()

        # Apply readout aggregation
        x = self._readout_aggregation(x, index=batch_index1)

        # Apply dense layers for classification
        x = self._apply_dense_layers(x)

        return x

    def test(self,
             y,
             y_hat: Optional[Any] = None,
             x: Union[Tensor, tuple[Tensor, Tensor]] = None,
             edge_index: Union[Tensor, tuple[Tensor, Tensor]] = None,
             batch_index: Tensor = None,
             x1: Optional[Tensor] = None,
             edge_index1: Optional[Tensor] = None,
             batch_index1: Optional[Tensor] = None,
             criterion: ClassificationLoss = MulticlassClassificationLoss(),
             top_k: Optional[int] = None,
             *args, **kwargs):
        if y_hat is None:
            y_hat = self(x=x, edge_index=edge_index, batch_index=batch_index, x1=x1, edge_index1=edge_index1,
                         batch_index1=batch_index1)

        return super(PairedProtMotionNet, self).test(y=y, y_hat=y_hat, x=None, edge_index=None, batch_index=None,
                                                     criterion=criterion, top_k=top_k)


class TransformerPairedProtMotionNet(PairedProtMotionNet):
    def __init__(self, encoder: SerializableModule, encoder_out_channels: int, dense_units: list[int],
                 dense_activations: list[str], dim_features: int, n_blocks: int, num_heads: int = 8,
                 kdim: Optional[int] = None, vdim: Optional[int] = None, dropout: float = 0.0,
                 readout: str = 'mean_pool', d_ff: Optional[int] = None, ff_activation: str = "gelu",
                 pre_norm: bool = True):
        super(TransformerPairedProtMotionNet, self).__init__(encoder=encoder, encoder_out_channels=encoder_out_channels,
                                                             dense_activations=dense_activations,
                                                             dim_features=dim_features, dense_units=dense_units,
                                                             num_heads=num_heads, kdim=kdim, vdim=vdim, dropout=dropout,
                                                             readout=readout)
        transformer_block = TransformerEncoderLayer(
            d_model=encoder_out_channels,
            nhead=num_heads,
            dim_feedforward=d_ff if d_ff is not None else encoder_out_channels,
            activation=self._ACTIVATIONS[ff_activation],
            norm_first=pre_norm,
            batch_first=False,
            device=None
        )
        self._transformer_encoder = TransformerEncoder(encoder_layer=transformer_block, num_layers=n_blocks,
                                                       norm=None, enable_nested_tensor=True)

        self.__d_ff: Optional[int] = d_ff
        self.__pre_norm: bool = pre_norm
        self.__ff_activation: str = ff_activation
        self.__n_blocks: int = n_blocks

    @property
    def d_ff(self) -> int:
        return self.__d_ff if self.__d_ff is not None else self.encoder_out_channels

    @property
    def pre_norm(self) -> bool:
        return self.__pre_norm

    @property
    def ff_activation(self) -> str:
        return self.__ff_activation

    @property
    def n_blocks(self) -> int:
        return self.__n_blocks

    def serialize_constructor_params(self, *args, **kwargs) -> dict:
        constructor_params = super(TransformerPairedProtMotionNet, self).serialize_constructor_params(*args, **kwargs)
        constructor_params.update({"d_ff": self.__d_ff, "pre_norm": self.__pre_norm,
                                   "ff_activation": self.__ff_activation, "n_blocks": self.__n_blocks})
        return constructor_params

    def forward(self, x: Union[Tensor, tuple[Tensor, Tensor]], edge_index: Union[Tensor, tuple[Tensor, Tensor]],
                batch_index: Tensor = None, x1: Optional[Tensor] = None, edge_index1: Optional[Tensor] = None,
                batch_index1: Optional[Tensor] = None, *args, **kwargs):

        # Get cross multi-head attention embeddings and masks
        x, key_padding_mask, batch_mask = self._get_multi_head_attention_embeddings(
            x=x,
            edge_index=edge_index,
            batch_index=batch_index,
            x1=x1,
            edge_index1=edge_index1,
            batch_index1=batch_index1, 
            *args,
            **kwargs
        )

        # Apply transformer encoder to further process the embeddings
        x = self._transformer_encoder(src=x, src_key_padding_mask=key_padding_mask)

        # Convert from (sequence_length, batch_size, embedding_size) to (batch_size, sequence_length, embedding_size)
        x = einops.rearrange(x, "s b f -> b s f")

        # Convert to PyG batch format again
        x, _ = from_dense_batch(dense_batch=x, mask=batch_mask)

        # Apply readout aggregation
        x = self._readout_aggregation(x, index=batch_index)

        # Apply dense layers for classification
        x = self._apply_dense_layers(x)

        return x


def train_step_paired_classifier(model: PairedProtMotionNet,
                                 train_data: PairedDataLoader,
                                 optimizer,
                                 device: torch.device,
                                 criterion: ClassificationLoss = MulticlassClassificationLoss(),
                                 logger: Optional[Logger] = None):
    # TODO: test this
    # Put the model in training mode
    model.train()

    # Running average loss over the batches
    running_loss = 0.0
    steps: int = 1

    for data in iter(train_data):

        # Reset the optimizer gradients
        optimizer.zero_grad()

        # Encoder output
        before: Data = data.a
        after: Data = data.b
        x = before.x.float().to(device)
        edge_index = before.edge_index.to(device)
        batch_index = before.batch.to(device)
        x1 = after.x.float().to(device)
        edge_index1 = after.edge_index.to(device)
        batch_index1 = after.batch.to(device)
        y_hat = model(
            x=x,
            edge_index=edge_index,
            batch_index=batch_index,
            x1=x1,
            edge_index1=edge_index1,
            batch_index1=batch_index1
        )
        del x, edge_index, edge_index1, batch_index, batch_index1, x1

        loss = model.loss(y=data.y.to(device), y_hat=y_hat, criterion=criterion, additional_terms=None)

        torch.cuda.empty_cache()

        # Gradient update
        loss.backward()

        # Advance the optimizer state
        optimizer.step()

        # Update running average loss
        running_loss = running_loss + 1 / steps * (loss.item() - running_loss)

        if logger is None:
            print(f"Steps: {steps}/{len(train_data)}, running loss {running_loss}")
        else:
            logger.log(f"Steps: {steps}/{len(train_data)}, running loss {running_loss}")
        steps += 1

        del loss
        del y_hat
        del data
        torch.cuda.empty_cache()

    return float(running_loss)


@torch.no_grad()
def test_step_paired_classifier(model: PairedProtMotionNet,
                                val_data: PairedDataLoader,
                                device: torch.device,
                                top_k: int = 3,
                                criterion: ClassificationLoss = MulticlassClassificationLoss()):
    # TODO: test this
    # put the model in evaluation mode
    model.eval()

    # Running average for loss, precision and AUC
    running_val_loss = 0
    running_precision = 0
    running_recall = 0
    running_accuracy = 0
    running_topk_acc = 0
    running_f1 = 0
    steps: int = 1

    for data in iter(val_data):
        # move batch to device
        # data = data.to(device)

        before: Data = data.a
        after: Data = data.b
        x = before.x.float().to(device)
        edge_index = before.edge_index.to(device)
        batch_index = before.batch.to(device)
        x1 = after.x.float().to(device)
        edge_index1 = after.edge_index.to(device)
        batch_index1 = after.batch.to(device)
        loss, acc, top_k_acc, prec, rec, f1 = model.test(
            y=data.y.to(device),
            y_hat=None,
            x=x,
            edge_index=edge_index,
            batch_index=batch_index,
            x1=x1,
            edge_index1=edge_index1,
            batch_index1=batch_index1,
            criterion=criterion,
            top_k=top_k
        )
        del x, edge_index, edge_index1, batch_index, batch_index1, x1
        torch.cuda.empty_cache()

        running_val_loss = running_val_loss + 1 / steps * (loss - running_val_loss)
        running_precision = running_precision + 1 / steps * (prec - running_precision)
        running_recall = running_recall + 1 / steps * (rec - running_recall)
        running_accuracy = running_accuracy + 1 / steps * (acc - running_accuracy)
        running_topk_acc = running_topk_acc + 1 / steps * (top_k_acc - running_topk_acc)
        running_f1 = running_f1 + 1 / steps * (f1 - running_f1)

        steps += 1

    return float(running_precision), float(running_recall), float(running_accuracy), float(running_topk_acc), \
        float(running_f1), float(running_val_loss)


def train_paired_classifier(model: PairedProtMotionNet,
                            train_data: PairedDataLoader,
                            val_data: PairedDataLoader,
                            epochs: int,
                            optimizer,
                            experiment_path: str,
                            experiment_name: str,
                            early_stopping_patience: int = EARLY_STOP_PATIENCE,
                            early_stopping_delta: float = 0,
                            top_k: int = 3,
                            logger: Optional[Logger] = None,
                            criterion: ClassificationLoss = MulticlassClassificationLoss(),
                            use_tensorboard_log: bool = False) -> (torch.nn.Module, dict):
    # TODO: test this
    # Move model to device
    device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
    model = model.to(device)

    experiment_path = os.path.join(experiment_path, experiment_name)
    os.makedirs(experiment_path, exist_ok=True)  # create experiment directory if it doesn't exist

    # Instantiate the summary writer
    writer = None
    if use_tensorboard_log:
        writer = SummaryWriter(f'{experiment_path}_{experiment_name}_{epochs}_epochs')

    # Early-stopping monitor
    checkpoint_path = os.path.join(f"{experiment_path}", "checkpoint.pt")
    monitor = EarlyStopping(
        patience=early_stopping_patience,
        verbose=True,
        delta=early_stopping_delta,
        path=checkpoint_path,
        trace_func=logger.log
    )

    # Metric history trace object
    mht = MetricsHistoryTracer(
        metrics=[
            "avg_precision",
            "avg_recall",
            "avg_accuracy",
            f"avg_top{top_k}_accuracy",
            "avg_f1",
            "val_loss",
            "train_loss"
        ],
        name="Classifier training metrics"
    )

    # Do validation step
    avg_precision, avg_recall, avg_accuracy, avg_topk_accuracy, avg_f1, val_loss = test_step_paired_classifier(
        model=model,
        val_data=val_data,
        device=device,
        top_k=top_k,
        criterion=criterion
    )
    torch.cuda.empty_cache()

    if logger is None:
        print(
            'Epoch: {:d}, Validation loss {:.4f}, Average accuracy: {:.4f}, '
            'Average top-{:d} accuracy: {:.4f}, Average precision: {:.4f}, Average recall: {:.4f}, '
            'Average F1: {:.4f}, '
            .format(0, val_loss, avg_accuracy, top_k,
                    avg_topk_accuracy, avg_precision, avg_recall, avg_f1)
        )
    else:
        logger.log(
            'Epoch: {:d}, Validation loss {:.4f}, Average accuracy: {:.4f}, '
            'Average top-{:d} accuracy: {:.4f}, Average precision: {:.4f}, Average recall: {:.4f}, '
            'Average F1: {:.4f}, '
            .format(0, val_loss, avg_accuracy, top_k,
                    avg_topk_accuracy, avg_precision, avg_recall, avg_f1)
        )

    # Tensorboard state update
    if use_tensorboard_log:
        writer.add_scalar('val_loss', val_loss, 0)
        writer.add_scalar('avg_precision', avg_precision, 0)
        writer.add_scalar('avg_recall', avg_recall, 0)
        writer.add_scalar('avg_accuracy', avg_accuracy, 0)
        writer.add_scalar(f'avg_top{top_k}_accuracy', avg_topk_accuracy, 0)
        writer.add_scalar('avg_f1', avg_f1, 0)

    # Check for early-stopping stuff
    monitor(val_loss, model)

    for epoch in range(0, epochs):
        # Do train step
        train_loss = train_step_paired_classifier(
            model=model,
            train_data=train_data,
            optimizer=optimizer,
            device=device,
            criterion=criterion,
            logger=None  # do not log epoch statistics to file
        )

        # Do validation step
        avg_precision, avg_recall, avg_accuracy, avg_topk_accuracy, avg_f1, val_loss = test_step_paired_classifier(
            model=model,
            val_data=val_data,
            device=device,
            top_k=top_k,
            criterion=criterion
        )

        torch.cuda.empty_cache()

        if logger is None:
            print(
                'Epoch: {:d}, Train loss: {:.4f}, Validation loss {:.4f}, Average accuracy: {:.4f}, '
                'Average top-{:d} accuracy: {:.4f}, Average precision: {:.4f}, Average recall: {:.4f}, '
                'Average F1: {:.4f}, '
                .format(epoch + 1, train_loss, val_loss, avg_accuracy, top_k,
                        avg_topk_accuracy, avg_precision, avg_recall, avg_f1)
            )
        else:
            logger.log(
                'Epoch: {:d}, Train loss: {:.4f}, Validation loss {:.4f}, Average accuracy: {:.4f}, '
                'Average top-{:d} accuracy: {:.4f}, Average precision: {:.4f}, Average recall: {:.4f}, '
                'Average F1: {:.4f}, '
                .format(epoch + 1, train_loss, val_loss, avg_accuracy, top_k,
                        avg_topk_accuracy, avg_precision, avg_recall, avg_f1)
            )

        # Tensorboard state update
        if use_tensorboard_log:
            writer.add_scalar('train_loss', train_loss, epoch + 1)
            writer.add_scalar('val_loss', val_loss, epoch + 1)
            writer.add_scalar('avg_precision', avg_precision, epoch + 1)
            writer.add_scalar('avg_recall', avg_recall, epoch + 1)
            writer.add_scalar('avg_accuracy', avg_accuracy, epoch + 1)
            writer.add_scalar(f'avg_top{top_k}_accuracy', avg_topk_accuracy, epoch + 1)
            writer.add_scalar('avg_f1', avg_f1, epoch + 1)

        # Check for early-stopping stuff
        monitor(val_loss, model)
        if monitor.early_stop:
            if logger is None:
                print(f"Epoch {epoch}: early stopping, restoring model checkpoint {checkpoint_path}...")
            else:
                logger.log(f"Epoch {epoch}: early stopping, restoring model checkpoint {checkpoint_path}...")
            break

        # Metrics history update
        mht.add_scalar('train_loss', train_loss)
        mht.add_scalar('val_loss', val_loss)
        mht.add_scalar('avg_precision', avg_precision)
        mht.add_scalar('avg_recall', avg_recall)
        mht.add_scalar('avg_accuracy', avg_accuracy)
        mht.add_scalar(f'avg_top{top_k}_accuracy', avg_topk_accuracy)
        mht.add_scalar('avg_f1', avg_f1)

    # Plot the metrics
    mht.plot_metrics(
        [
            'train_loss',
            'val_loss',
        ],
        figsize=FIGURE_SIZE_DEFAULT,
        traced_min_metric='val_loss',
        store_path=os.path.join(f"{experiment_path}", "loss.svg")
    )

    mht.plot_metrics(
        [
            "avg_precision",
            "avg_recall",
            "avg_f1",
        ],
        figsize=FIGURE_SIZE_DEFAULT,
        traced_max_metric='avg_f1',
        store_path=os.path.join(f"{experiment_path}", "prec_rec_f1.svg")
    )

    mht.plot_metrics(
        [
            'avg_accuracy',
        ],
        figsize=FIGURE_SIZE_DEFAULT,
        traced_max_metric='avg_accuracy',
        store_path=os.path.join(f"{experiment_path}", "avg_accuracy.svg")
    )

    mht.plot_metrics(
        [
            f'avg_top{top_k}_accuracy',
        ],
        figsize=FIGURE_SIZE_DEFAULT,
        traced_max_metric=f'avg_top{top_k}_accuracy',
        store_path=os.path.join(f"{experiment_path}", f'avg_top{top_k}_accuracy.svg')
    )

    # Load best model
    model.load_state_dict(torch.load(checkpoint_path))

    avg_precision, avg_recall, avg_accuracy, avg_topk_accuracy, avg_f1, val_loss = test_step_paired_classifier(
        model=model,
        val_data=val_data,
        device=device,
        top_k=top_k,
        criterion=criterion
    )
    metrics = {
        "precision": avg_precision,
        "recall": avg_recall,
        "accuracy": avg_accuracy,
        "avg_topk_accuracy": avg_topk_accuracy,
        "f1": avg_f1,
        "val_loss": val_loss
    }
    return model, metrics
