import os
import json
import tempfile
import base64
from typing import Optional
from transmission_rpc import Client, TransmissionError


class TransmissionClient:
    def __init__(self, host: str, port: int, username: str = "", 
                 password: str = "", timeout: int = 30):
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.timeout = timeout

    def connect(self) -> Client:
        """Создаёт и возвращает клиент Transmission RPC."""
        try:
            client = Client(
                host=self.host,
                port=self.port,
                username=self.username if self.username else None,
                password=self.password if self.password else None,
                timeout=self.timeout
            )
            # Проверка соединения
            client.session_stats()
            return client
        except TransmissionError as e:
            raise Exception(f"Ошибка подключения к Transmission: {e}")
        except Exception as e:
            raise Exception(f"Не удалось подключиться к Transmission: {e}")

    def test_connection(self) -> tuple:
        """Проверяет соединение. Возвращает (ok, message)."""
        try:
            client = self.connect()
            session = client.get_session()
            version = session.version
            return True, f"Подключено к Transmission {version}"
        except Exception as e:
            return False, str(e)

    def add_torrent(self, torrent_data: bytes, download_dir: str = None,
                    paused: bool = False) -> dict:
        """Добавляет торрент в Transmission через временный файл.
        
        Args:
            torrent_data: содержимое .torrent файла (bytes)
            download_dir: путь для скачивания (None = использовать дефолт)
            paused: добавить на паузу
        
        Returns:
            dict с информацией о торренте
        """
        tmp_path = None
        try:
            client = self.connect()
            
            # Сохраняем во временный файл
            with tempfile.NamedTemporaryFile(mode='wb', suffix='.torrent', delete=False) as tmp:
                tmp.write(torrent_data)
                tmp_path = tmp.name
            
            print(f"[transmission] Saved torrent to temp file: {tmp_path}")
            
            # Передаём путь к файлу
            torrent = client.add_torrent(
                torrent=tmp_path,
                download_dir=download_dir,
                paused=paused
            )
            
            result = {
                'hash': torrent.hashString,
                'name': torrent.name,
                'size': torrent.total_size,
                'status': 'added'
            }
            
            print(f"[transmission] Added torrent: {result['name']} ({result['hash']})")
            return result
            
        except TransmissionError as e:
            error_msg = str(e)
            print(f"[transmission] TransmissionError: {error_msg}")
            raise Exception(f"Ошибка добавления торрента: {error_msg}")
        except Exception as e:
            error_msg = str(e)
            print(f"[transmission] Unexpected error: {error_msg}")
            raise Exception(f"Не удалось добавить торрент: {error_msg}")
        finally:
            # Удаляем временный файл
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                    print(f"[transmission] Removed temp file: {tmp_path}")
                except Exception as e:
                    print(f"[transmission] Failed to remove temp file: {e}")

    def get_torrent_status(self, torrent_hash: str) -> Optional[dict]:
        """Получает статус торрента по хэшу.
        
        Returns:
            dict с информацией о статусе или None если не найден
        """
        try:
            client = self.connect()
            torrent = client.get_torrent(torrent_hash)
            
            return {
                'hash': torrent.hashString,
                'name': torrent.name,
                'status': torrent.status,
                'progress': torrent.progress,
                'size': torrent.total_size,
                'downloaded': torrent.downloaded_ever,
                'rate_down': torrent.rate_download,
                'rate_up': torrent.rate_upload,
                'eta': torrent.eta.seconds if torrent.eta else None,
                'is_finished': torrent.is_finished
            }
        except TransmissionError:
            return None
        except Exception as e:
            print(f"[transmission] Error getting status: {e}")
            return None

    def remove_torrent(self, torrent_hash: str, delete_data: bool = False) -> bool:
        """Удаляет торрент из Transmission."""
        try:
            client = self.connect()
            client.remove_torrent(torrent_hash, delete_data=delete_data)
            return True
        except Exception as e:
            print(f"[transmission] Error removing torrent: {e}")
            return False