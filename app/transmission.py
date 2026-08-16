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
        """Добавляет торрент в Transmission через base64.
        
        Args:
            torrent_data: содержимое .torrent файла (bytes)
            download_dir: путь для скачивания (None = использовать дефолт)
            paused: добавить на паузу
        
        Returns:
            dict с информацией о торренте
        """
        try:
            client = self.connect()
            
            # Проверяем что данные выглядят как .torrent
            if not torrent_data.startswith(b'd8:'):
                print(f"[transmission] WARNING: torrent data doesn't start with 'd8:'")
                print(f"[transmission] First 50 bytes: {torrent_data[:50]}")
            
            # Кодируем в base64
            torrent_b64 = base64.b64encode(torrent_data).decode('ascii')
            print(f"[transmission] Encoded torrent to base64: {len(torrent_b64)} chars")
            
            # Используем параметр metainfo для base64-encoded данных
            kwargs = {
                'metainfo': torrent_b64,
                'paused': paused
            }
            if download_dir:
                kwargs['download_dir'] = download_dir
            
            torrent = client.add_torrent(**kwargs)
            
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
            # Добавляем диагностику
            print(f"[transmission] Torrent data size: {len(torrent_data)} bytes")
            print(f"[transmission] Torrent data starts with: {torrent_data[:20]}")
            raise Exception(f"Ошибка добавления торрента: {error_msg}")
        except Exception as e:
            error_msg = str(e)
            print(f"[transmission] Unexpected error: {error_msg}")
            import traceback
            traceback.print_exc()
            raise Exception(f"Не удалось добавить торрент: {error_msg}")

    def get_torrent_status(self, torrent_hash: str) -> Optional[dict]:
        """Получает статус торрента по хэшу."""
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