from django.urls import path
from rest_framework_simplejwt.views import TokenRefreshView

from . import auth_views, feedback_views, views

app_name = 'c360'

urlpatterns = [
    path('meta/', views.MetaView.as_view(), name='meta'),
    # --- auth: JWT + login 2FA (admin-provisioned users; no self-service) ---
    # Sign-in is two-step: login/ verifies the password and emails a code; login/verify/
    # exchanges the code for tokens. (No single-step token endpoint, so 2FA can't be
    # bypassed.) token/refresh/ powers the SPA's silent refresh.
    path('auth/login/', auth_views.LoginStartView.as_view(), name='auth-login'),
    path('auth/login/verify/', auth_views.LoginVerifyView.as_view(), name='auth-login-verify'),
    path('auth/token/refresh/', TokenRefreshView.as_view(), name='token-refresh'),
    path('auth/password-reset/', auth_views.PasswordResetRequestView.as_view(), name='auth-password-reset'),
    path('auth/password-reset/confirm/', auth_views.PasswordResetConfirmView.as_view(), name='auth-password-reset-confirm'),
    path('auth/logout/', auth_views.LogoutAPIView.as_view(), name='auth-logout'),
    path('auth/me/', auth_views.MeView.as_view(), name='auth-me'),
    # --- admin user management (staff/superuser only) ---
    path('auth/users/', auth_views.AdminUserListCreateView.as_view(), name='admin-user-list'),
    path('auth/users/<int:pk>/', auth_views.AdminUserDetailView.as_view(), name='admin-user-detail'),
    path('auth/users/<int:pk>/set-password/', auth_views.AdminSetPasswordView.as_view(), name='admin-user-set-password'),
    path('auth/roles/', auth_views.RoleListView.as_view(), name='admin-role-list'),
    path('auth/user-meta/', auth_views.UserMetaChoicesView.as_view(), name='admin-user-meta'),
    path('customers/', views.CustomerListView.as_view(), name='customer-list'),
    path('customers/<str:cust_id>/', views.CustomerDetailView.as_view(), name='customer-detail'),
    path('customers/<str:cust_id>/overview/', views.CustomerOverviewView.as_view(), name='customer-overview'),
    path('customers/<str:cust_id>/domains/hfcb/', views.HFCBDomainView.as_view(), name='hfcb-domain'),
    path('customers/<str:cust_id>/domains/<str:domain>/', views.DomainView.as_view(), name='domain'),
    path('customers/<str:cust_id>/recommendations/', views.RecommendationsView.as_view(), name='recommendations'),
    # --- recommendation outcome-logging (the feedback loop) ---
    path('recommendations/feedback/', feedback_views.FeedbackView.as_view(), name='rec-feedback'),
    path('recommendations/feedback/stats/', feedback_views.FeedbackStatsView.as_view(), name='rec-feedback-stats'),
    path('portfolio/overview/', views.PortfolioOverviewView.as_view(), name='portfolio-overview'),
    path('portfolio/worklist/', views.WorklistView.as_view(), name='worklist'),
]
