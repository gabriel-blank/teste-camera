import time
import cv2
import os
from anomalib.data import Folder
from anomalib.models import Patchcore
from anomalib.engine import Engine
from lightning.pytorch import seed_everything

from utils.camera_stream import BufferedVideoStream


def create_dataset(duration_sec: int, stream: BufferedVideoStream, dataset_name: str):
    """
    Captura frames do stream por duration_sec segundos e salva em pasta dataset.

    Args:
        duration_sec: Duração em segundos para capturar frames
        stream: Stream de vídeo (BufferedVideoStream)

    Returns:
        str: Caminho da pasta dataset criada
    """
    # Cria pasta dataset
    dataset_path = f"dataset/{dataset_name}"

    # Cria estrutura de pastas
    os.makedirs(dataset_path, exist_ok=True)

    print(f"Capturando frames por {duration_sec} segundos...")
    print(f"Salvando em: {dataset_path}")

    # Contadores
    frame_count = 0
    start_time = time.time()

    try:
        while time.time() - start_time < duration_sec:
            # Lê frame do stream
            frame = stream.read()

            if frame is not None:
                # Salva frame
                frame_filename = f"{dataset_name}_frame_{frame_count:06d}.jpg"
                frame_path = os.path.join(dataset_path, frame_filename)

                cv2.imwrite(frame_path, frame, [cv2.IMWRITE_JPEG_QUALITY, 90])

                cv2.waitKey(1)
                frame_count += 1

            time.sleep(0.01)

    except KeyboardInterrupt:
        print("\nCaptura interrompida pelo usuário.")

    print(f"\nCaptura concluída!")
    print(f"Total de frames capturados: {frame_count}")
    print(f"Tempo real de captura: {time.time() - start_time:.1f}s")
    print(f"Dataset salvo em: {dataset_path}")

    return dataset_path


def train_model(dataset_path: str, model_name: str):
    seed_everything(42)

    datamodule = Folder(
        name=model_name,
        normal_dir=dataset_path,
        seed=42,
        val_split_ratio=0.2,
    )

    datamodule.setup()

    model = Patchcore(backbone="resnet50")

    engine = Engine(
        default_root_dir="model",
        callbacks=[],
    )

    engine.fit(model=model, datamodule=datamodule)
