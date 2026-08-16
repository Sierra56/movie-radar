import os
import json
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
        """Добавляет торрент в Transmission.
        
        Args:
            torrent_data: содержимое .torrent файла
            download_dir: путь для скачивания (None = использовать дефолт)
            paused: добавить на паузу
        
        Returns:
            dict с информацией о торренте
        """
        try:
            client = self.connect()
            
            # transmission-rpc принимает либо путь к файлу, либо magnet, либо base64
            # Для bytes используем base64
            import base64
            torrent_b64 = base64.b64encode(torrent_data).decode('utf-8')
            
            torrent = client.add_torrent(
                torrent_b64,
                download_dir=download_dir,
                paused=paused
            )
            
            return {
                'hash': torrent.hashString,
                'name': torrent.name,
                'size': torrent.total_size,
                'status': 'added'
            }
        except TransmissionError as e:
            raise Exception(f"Ошибка добавления торрента: {e}")
        except Exception as e:
            raise Exception(f"Не удалось добавить торрент: {e}")

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