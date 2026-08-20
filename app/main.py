from datetime import datetime, timedelta

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from .core import POSTERS_DIR, STATIC_DIR, scheduler, get_refresh_hours
from .catalog import refresh_catalog
from .notify import schedule_telegram_job
from .jobs import (schedule_distribution_job, schedule_transmission_poll_job,
                   schedule_auto_clean_job)
from .web import router_pages, router_settings, router_dist
from .backup import router_backup

app = FastAPI()

app.mount("/posters", StaticFiles(directory=POSTERS_DIR), name="posters")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

scheduler.add_job(refresh_catalog, "interval", hours=get_refresh_hours(),
                  id="refresh", next_run_time=None)

app.include_router(router_pages)
app.include_router(router_settings)
app.include_router(router_dist)
app.include_router(router_backup)


@app.on_event("startup")
async def on_startup():
    scheduler.start()
    scheduler.reschedule_job("refresh", trigger="interval", hours=get_refresh_hours())
    scheduler.modify_job("refresh", next_run_time=datetime.now() + timedelta(minutes=5))
    schedule_telegram_job()
    schedule_distribution_job()
    schedule_transmission_poll_job()
    schedule_auto_clean_job()


@app.on_event("shutdown")
async def on_shutdown():
    scheduler.shutdown()