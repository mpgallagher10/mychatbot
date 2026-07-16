web: python manage.py migrate --noinput && gunicorn config.wsgi --bind 0.0.0.0:$PORT --workers 2 --timeout 120
worker: python manage.py runworker
