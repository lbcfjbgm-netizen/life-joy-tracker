import os
import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")
django.setup()

from django.contrib.auth import get_user_model

User = get_user_model()
username = os.environ.get("SU_USERNAME", "admin")
password = os.environ.get("SU_PASSWORD", "admin123456")
email = os.environ.get("SU_EMAIL", "admin@example.com")

u, created = User.objects.get_or_create(username=username)
u.email = email
u.is_staff = True
u.is_superuser = True
u.set_password(password)
u.save()
print("SUPERUSER", "created" if created else "updated", username)
print("check_password:", u.check_password(password))
print("total users:", User.objects.count())
