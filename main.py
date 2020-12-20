import pandas as pd
import torch
import os

from torch.nn import CrossEntropyLoss
from torch.nn.functional import one_hot
from torch.utils.data import DataLoader, random_split
from torch.nn.utils.rnn import pad_sequence
from datetime import timedelta
from tqdm import tqdm
from pprint import pformat

from nn import RNN
from config import (
    BATCH_SIZE,
    BASE_PATH,
    DB_CONNECTION_STRING,
    DEBUG,
    DEBUG_FOLDER,
    DATA_DIR,
    DATASET,
    EPOCHS,
    HIDDEN_SIZE,
    EMBEDDING_SIZE,
    LEARNING_RATE,
    NUM_LAYERS,
    USE_CATEGORY_SIMILARITY,
    MAX_TEST_SIZE,
    HIDDEN_DROPOUT,
    INPUT_DROPOUT,
    SIMILARITY_THRESHOLD,
)
from dataset import SequenceDataset
from utils import (
    get_timestamp,
    print_line_separator,
    mkdir_p,
)

SPLIT_SESSIONS = SIMILARITY_THRESHOLD > 0


def collate_fn(session_data):
    # convert to input and label tensors + metadata
    transformed_sessions = []
    for s, session_id in session_data:
        # input = dataset.split_session(s[:-1]) if SPLIT_SESSIONS else s[:-1]
        input = dataset.filter_session(s[:-1]) if SPLIT_SESSIONS else s[:-1]
        input = torch.tensor(input, dtype=torch.long)
        label = torch.tensor(s[-1])
        metadata = (session_id, len(input))
        transformed_sessions.append((input, label, metadata))

    inputs, labels, metadata = zip(*transformed_sessions)

    inputs = pad_sequence(inputs, batch_first=True, padding_value=0).to(device)
    labels = torch.stack(labels)

    return inputs, labels, metadata


# load data
dataset_path = os.path.join(BASE_PATH, DATA_DIR, DATASET)
dataset = SequenceDataset(dataset_path)
test_size = min(int(0.2 * len(dataset)), MAX_TEST_SIZE)
train_size = len(dataset) - test_size
train, test = random_split(dataset, [train_size, test_size])
dataloaders = {
    "train": DataLoader(
        train, batch_size=BATCH_SIZE, shuffle=True, collate_fn=collate_fn
    ),
    "test": DataLoader(
        test, batch_size=BATCH_SIZE, shuffle=True, collate_fn=collate_fn
    ),
}
data_sizes = {"train": train_size, "test": test_size}

print(f"Train size: {train_size} sessions, test size: {test_size} sessions")


# select device
device_name = "cuda" if torch.cuda.is_available() else "cpu"
device = torch.device(device_name)
print(f"Running on {device_name}")

# load embeddings
pretrained_embeddings = dataset.get_item_embeddings()

# create model
model = RNN(
    vocab_size=dataset.item_count,
    embedding_size=EMBEDDING_SIZE,
    hidden_size=HIDDEN_SIZE,
    batch_size=BATCH_SIZE,
    num_layers=NUM_LAYERS,
    input_dropout=INPUT_DROPOUT,
    hidden_dropout=HIDDEN_DROPOUT,
    device=device,
    pretrained_embeddings=pretrained_embeddings,
)
loss_function = CrossEntropyLoss()
optimizer = torch.optim.Adam(model.parameters(), lr=LEARNING_RATE)


# calculate baseline
print_line_separator()
print("Calculating baseline scores - most popular")
popular_acc = 0
popular_hits_10 = 0
for _, labels, _ in tqdm(dataloaders["test"]):
    popular_hits = sum([l == dataset.most_popular_items[0] for l in labels.tolist()])
    popular_hits_10 += sum([l in dataset.most_popular_items for l in labels.tolist()])
popular_acc = popular_hits / data_sizes["test"] * 100
popular_acc_10 = popular_hits_10 / data_sizes["test"] * 100
print(f"Baseline - acc@1: {popular_acc:.4f}, acc@10: {popular_acc_10:.4f}\n")
print_line_separator()


# train
def train(dataloaders, epochs=10, debug=False):
    print(f"Training")

    for epoch in range(1, epochs + 1):
        print(f"Epoch: {epoch} / {epochs}\n")

        if debug:
            debug_path = os.path.join(BASE_PATH, DEBUG_FOLDER, f"epoch_{epoch}.txt")
            debug_dir_path = os.path.dirname(debug_path)
            mkdir_p(debug_dir_path)
            debug_f = open(debug_path, "w")

        for phase in ["train", "test"]:
            print(f"Phase: {phase}")

            is_train = phase == "train"

            if is_train:
                model.train()
            else:
                model.eval()

            with torch.set_grad_enabled(is_train):
                hits = 0
                hits_10 = 0
                long_hits = 0
                long_hits_10 = 0
                long_session_count = 0
                running_loss = 0

                for i, (inputs, labels, metadata) in enumerate(
                    tqdm(dataloaders[phase])
                ):
                    inputs = inputs.to(device, dtype=torch.float)
                    labels = labels.to(device)
                    session_ids, lengths = zip(*metadata)

                    optimizer.zero_grad()
                    model.reset_hidden()

                    y_pred = model(inputs, lengths)

                    loss = loss_function(y_pred, labels)

                    curr_batch_size = inputs.shape[0]
                    if is_train:
                        loss.backward()
                        optimizer.step()
                    else:
                        # calculate hits@10 - only for test as it would slow down training
                        predicted_indexes_10 = torch.topk(y_pred, 10, axis=1).indices
                        hits_10 += sum(
                            [
                                l in predicted_indexes_10[i]
                                for i, l in enumerate(labels.tolist())
                            ]
                        )

                        # show the predictions
                        if debug:
                            for session_id, predictions in zip(
                                session_ids, predicted_indexes_10
                            ):
                                session = [
                                    dataset.idx_to_info(i)
                                    for i in dataset.sessions.loc[session_id]
                                ]
                                predictions = [
                                    dataset.idx_to_info(i) for i in predictions.tolist()
                                ]

                                debug_f.write(
                                    (
                                        f"INPUT:\n{pformat(session[:-1], width=160)}\n\n"
                                        f"LABEL:\n{pformat(session[-1], width=160)}\n\n"
                                        f"PREDICTIONS:\n{pformat(predictions, width=160)}\n\n"
                                        f"long: {session_id in dataset.long_session_ids}\n"
                                        f"correct: {session[-1] in predictions}\n"
                                        f"{'-' * 72}\n"
                                    )
                                )

                        # calculate hits@10 for long sessions only
                        long_indexes = [
                            i in dataset.long_session_ids for i in session_ids
                        ]
                        long_inputs = inputs[long_indexes]
                        long_labels = labels[long_indexes]
                        long_preds = y_pred[long_indexes]

                        predicted_indexes_10 = torch.topk(
                            long_preds, 10, axis=1
                        ).indices
                        long_hits_10 += sum(
                            [
                                l in predicted_indexes_10[i]
                                for i, l in enumerate(long_labels.tolist())
                            ]
                        )
                        long_session_count += long_inputs.shape[0]

                    predicted_indexes = torch.argmax(y_pred, 1)
                    hits += torch.sum(predicted_indexes == labels).item()
                    running_loss += loss.item() * curr_batch_size

            avg_loss = running_loss / data_sizes[phase]
            acc = hits / data_sizes[phase] * 100
            if is_train:
                print(f"Avg. loss: {avg_loss:.8f}, acc@1: {acc:.4f}\n")
            else:
                acc_10 = hits_10 / data_sizes[phase] * 100
                long_acc_10 = (
                    long_hits_10 / long_session_count * 100
                    if long_session_count > 0
                    else 999
                )
                print(
                    f"Avg. loss: {avg_loss:.8f}, acc@1: {acc:.4f}, acc@10: {acc_10:.4f}, long acc@10: {long_acc_10:.4f}\n"
                )

        if debug:
            debug_f.close()

        print_line_separator()


train(dataloaders, epochs=EPOCHS, debug=DEBUG)
