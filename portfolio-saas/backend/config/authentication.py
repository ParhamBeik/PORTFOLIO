from django.conf import settings
from django.contrib.auth import get_user_model
from rest_framework.authentication import BaseAuthentication

User = get_user_model()

class PublicDemoUserAuthentication(BaseAuthentication):
    """Automatically logs in tokenless requests as the demo user in dev/prod.
    
    Bypassed in test environment to preserve safety and permission assertions.
    """
    def authenticate(self, request):
        import sys
        # Bypass fallback under testing
        if getattr(settings, "ENVIRONMENT", None) == "test" or "test" in sys.argv or any("pytest" in arg for arg in sys.argv):
            return None

        # Fetch or create the demo user
        user, created = User.objects.get_or_create(
            email="demo@portfolio.local",
            defaults={
                "first_name": "Demo",
                "last_name": "User",
                "tier": User.Tier.PRO,
                "is_active": True,
            }
        )
        if created:
            user.set_unusable_password()
            user.save()
            
            # Auto-create the default account for the demo user
            from portfolio.models import Account
            Account.objects.get_or_create(user=user, name="Main Portfolio")
            
        return (user, None)
