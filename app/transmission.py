import os
import json
import tempfile
import asyncio
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

    def _safe_get(self, torrent, attr: str, default=None):
        """Безопасно получает атрибут торрента."""
        try:
            return getattr(torrent, attr, default)
        except Exception:
            return default

    def add_torrent(self, torrent_data: bytes, download_dir: str = None,
                    paused: bool = False) -> dict:
        """Добавляет торрент в Transmission.
        
        Использует bytes напрямую (поддерживается в transmission-rpc v4+).
        """
        client = self.connect()
        
        # Собираем параметры
        kwargs = {'paused': paused}
        if download_dir:
            kwargs['download_dir'] = download_dir
        
        try:
            print(f"[transmission] Adding torrent ({len(torrent_data)} bytes)")
            torrent = client.add_torrent(torrent_data, **kwargs)
            
            # Получаем hash — он всегда доступен
            torrent_hash = self._safe_get(torrent, 'hashString') or \
                           self._safe_get(torrent, 'hash')
            torrent_name = self._safe_get(torrent, 'name') or \
                           self._safe_get(torrent, 'torrent_name') or 'unknown'
            
            print(f"[transmission] Added: {torrent_name} ({torrent_hash})")
            
            # Делаем повторный запрос для получения полной информации
            # (некоторые поля недоступны сразу после добавления)
            torrent_size = 0
            try:
                # Небольшая задержка чтобы Transmission обработал торрент
                import time
                time.sleep(0.5)
                
                fresh_torrent = client.get_torrent(torrent_hash)
                torrent_size = (self._safe_get(fresh_torrent, 'total_size') or
                                self._safe_get(fresh_torrent, 'totalSize') or
                                self._safe_get(fresh_torrent, 'sizeWhenDone') or
                                0)
                # Обновляем имя если получили
                fresh_name = self._safe_get(fresh_torrent, 'name')
                if fresh_name:
                    torrent_name = fresh_name
                print(f"[transmission] Got full info: size={torrent_size}")
            except Exception as e:
                print(f"[transmission] Could not get full info: {e}")
            
            return {
                'hash': torrent_hash,
                'name': torrent_name,
                'size': torrent_size,
                'status': 'added',
            }
            
        except TransmissionError as e:
            error_msg = str(e)
            print(f"[transmission] TransmissionError: {error_msg}")
            raise Exception(f"Ошибка добавления торрента: {error_msg}")
        except Exception as e:
            error_msg = str(e)
            print(f"[transmission] Unexpected error: {type(e).__name__}: {error_msg}")
            import traceback
            traceback.print_exc()
            raise Exception(f"Не удалось добавить торрент: {error_msg}")

    def get_torrent_status(self, torrent_hash: str) -> Optional[dict]:
        """Получает статус торрента по хэшу."""
        try:
            client = self.connect()
            torrent = client.get_torrent(torrent_hash)
            
            return {
                'hash': self._safe_get(torrent, 'hashString', torrent_hash),
                'name': self._safe_get(torrent, 'name', 'unknown'),
                'status': self._safe_get(torrent, 'status', 'unknown'),
                'progress': self._safe_get(torrent, 'progress', 0),
                'size': (self._safe_get(torrent, 'total_size') or
                         self._safe_get(torrent, 'totalSize') or
                         self._safe_get(torrent, 'sizeWhenDone') or 0),
                'downloaded': self._safe_get(torrent, 'downloadedEver', 0),
                'rate_down': self._safe_get(torrent, 'rateDownload', 0),
                'rate_up': self._safe_get(torrent, 'rateUpload', 0),
                'eta': (self._safe_get(torrent, 'eta') or 
                        self._safe_get(torrent, 'eta_seconds') or 0),
                'is_finished': self._safe_get(torrent, 'isFinished', False),
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