import os

bind = "0.0.0.0:{}".format(os.getenv("PORT", "8080"))
workers = 1
threads = 8
timeout = 60


def post_worker_init(worker):
    """Modo dev local (DEV_LOOP=1): arranca el loop de pull porque no hay
    Cloud Tasks que despierte al worker. En prod queda apagado: el push de
    Cloud Tasks a POST /run es el que trae trabajo."""
    if os.getenv("DEV_LOOP") == "1":
        from worker import start_dev_loop
        start_dev_loop()
