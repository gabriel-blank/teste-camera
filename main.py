from utils.api_controller import ApiController
from patches import patch_linked_dir, patch_predict_dataset
import yaml
from model.anomaly_model_training import create_dataset, train_model
from model.classifier_model_training import train_svm_end_to_end
from model.inference_loop import run_inference
import time
import threading

from utils.logger import logger


def main():
    with open("config.yaml", "r") as f:
        config = yaml.safe_load(f)

    # train_svm_end_to_end(api, [1, 2, 3], base_url_prefix=config["api"]["url"])

    stop_ev = threading.Event()
    for camera in config["cameras"]:

        thread = threading.Thread(
            target=run_inference,
            args=(
                camera["url"],
                camera["po"],
                f"model/Patchcore/teste1/weights/lightning/model.ckpt",
                ApiController(config=config["api"]),
                "model/svm/svm_model.joblib",
                stop_ev,
            ),
            daemon=True,
            name=f"inference_{camera['po']}",
        )
        thread.start()

    # dataset_path = create_dataset(
    #     duration_sec=config["train_duration_sec"],
    #     stream=stream,
    #     dataset_name=name,
    # )

    # train_model(
    #     dataset_path=f"dataset_patchcore/normal",
    #     model_name="retrain1"
    # )

    # Loop infinito para manter a thread principal em execução
    try:
        while True:
            time.sleep(5)
    except KeyboardInterrupt:
        logger.info("Interrompido pelo usuário")


if __name__ == "__main__":
    main()
