import cv2
import os
import time
from pathlib import Path


def extract_frames(video_path, output_dir, fps_limit=None, quality=90):
    """
    Extrai frames de um vídeo e salva como imagens.

    Args:
        video_path: Caminho para o vídeo
        output_dir: Pasta onde salvar os frames
        fps_limit: Limite de FPS para extração (None = extrair todos os frames)
        quality: Qualidade da imagem JPEG (1-100)
    """
    # Verifica se o vídeo existe
    if not os.path.exists(video_path):
        print(f"Erro: Vídeo não encontrado em {video_path}")
        return

    # Cria pasta de saída
    os.makedirs(output_dir, exist_ok=True)

    # Abre o vídeo
    cap = cv2.VideoCapture(video_path)

    if not cap.isOpened():
        print(f"Erro: Não foi possível abrir o vídeo {video_path}")
        return

    # Obtém informações do vídeo
    total_frames = int(cap.get(cv2.CAP_PROP_FRAME_COUNT))
    fps = cap.get(cv2.CAP_PROP_FPS)
    duration = total_frames / fps

    print(f"Informações do vídeo:")
    print(f"  - Total de frames: {total_frames}")
    print(f"  - FPS: {fps:.2f}")
    print(f"  - Duração: {duration:.2f} segundos")
    print(f"  - Pasta de saída: {output_dir}")

    # Calcula intervalo entre frames se fps_limit for especificado
    frame_interval = 1
    if fps_limit:
        frame_interval = max(1, int(fps / fps_limit))
        print(
            f"  - Extraindo 1 frame a cada {frame_interval} frames (FPS limitado a {fps_limit})"
        )

    # Contadores
    frame_count = 0
    saved_count = 0
    start_time = time.time()

    try:
        while True:
            ret, frame = cap.read()

            if not ret:
                break

            # Salva frame se estiver no intervalo correto
            if frame_count % frame_interval == 0:
                # Nome do arquivo com timestamp
                timestamp = frame_count / fps
                frame_filename = f"frame_{frame_count:06d}_{timestamp:.3f}s.jpg"
                frame_path = os.path.join(output_dir, frame_filename)

                # Salva frame
                cv2.imwrite(frame_path, frame, [cv2.IMWRITE_JPEG_QUALITY, quality])
                saved_count += 1

                # Mostra progresso a cada 50 frames salvos
                if saved_count % 50 == 0:
                    elapsed = time.time() - start_time
                    print(f"Extraídos {saved_count} frames em {elapsed:.1f}s")

            frame_count += 1

    except KeyboardInterrupt:
        print("\nExtração interrompida pelo usuário.")

    finally:
        cap.release()

    # Estatísticas finais
    total_time = time.time() - start_time
    print(f"\nExtração concluída!")
    print(f"  - Frames processados: {frame_count}")
    print(f"  - Frames salvos: {saved_count}")
    print(f"  - Tempo total: {total_time:.1f}s")
    print(f"  - FPS de processamento: {frame_count / total_time:.2f}")
    print(f"  - Frames salvos em: {output_dir}")


def main():
    """Função principal com diferentes opções de extração."""
    video_path = "video/video_tear.mp4"
    base_output_dir = "extracted_frames"

    print("=== Extrator de Frames ===")
    print(f"Vídeo: {video_path}")
    print()

    extract_frames(video_path, f"{base_output_dir}/5fps", fps_limit=3)
    print()

    print("Extração concluída! Verifique as pastas:")
    print(f"  - {base_output_dir}/5fps/ (5 frames por segundo)")


if __name__ == "__main__":
    main()
