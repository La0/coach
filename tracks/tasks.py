from __future__ import absolute_import

from celery import shared_task, task

@shared_task
def tracks_import(*args, **kwargs):
  '''
  Import all new Tracks
  '''
  from users.models import Athlete
  from tracks.providers import all_providers

  users = Athlete.objects.all()
  users = users.order_by('pk')
  for user in users:
    if not user.is_premium:
      continue
    for provider in all_providers(user):
      if not provider.is_connected():
        continue

      # Start a subtask per import
      provider_import.subtask((provider, )).apply_async()

@task
def provider_import(provider):
  # Helper to run a provider import
  provider.import_user()
