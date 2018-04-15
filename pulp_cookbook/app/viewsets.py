# (C) Copyright 2018 Simon Baatz <gmbnomis@gmail.com>
#
# SPDX-License-Identifier: GPL-2.0-or-later

from gettext import gettext as _

from django.db import transaction
from django_filters.rest_framework import filterset
from rest_framework.decorators import detail_route
from rest_framework import serializers, status
from rest_framework.response import Response

from pulpcore.plugin.models import Artifact, Repository, RepositoryVersion

from pulpcore.plugin.viewsets import (
    ContentViewSet,
    RemoteViewSet,
    OperationPostponedResponse,
    PublisherViewSet)
from rest_framework_nested.relations import NestedHyperlinkedRelatedField

from . import tasks
from .models import CookbookPackageContent, CookbookRemote, CookbookPublisher
from .serializers import (
    CookbookPackageContentSerializer,
    CookbookRemoteSerializer,
    CookbookPublisherSerializer)

from pulp_cookbook.metadata import CookbookMetadata


class CookbookPackageContentFilter(filterset.FilterSet):
    class Meta:
        model = CookbookPackageContent
        fields = [
            'name',
            'version'
        ]


class _RepositoryPublishURLSerializer(serializers.Serializer):

    repository = serializers.HyperlinkedRelatedField(
        help_text=_('A URI of the repository to be published.'),
        required=False,
        label=_('Repository'),
        queryset=Repository.objects.all(),
        view_name='repositories-detail',
    )

    repository_version = NestedHyperlinkedRelatedField(
        help_text=_('A URI of the repository version to be published.'),
        required=False,
        label=_('Repository Version'),
        queryset=RepositoryVersion.objects.all(),
        view_name='versions-detail',
        lookup_field='number',
        parent_lookup_kwargs={'repository_pk': 'repository__pk'},
    )

    def validate(self, data):
        repository = data.get('repository')
        repository_version = data.get('repository_version')

        if not repository and not repository_version:
            raise serializers.ValidationError(
                _("Either the 'repository' or 'repository_version' need to be specified"))
        elif not repository and repository_version:
            return data
        elif repository and not repository_version:
            version = RepositoryVersion.latest(repository)
            if version:
                new_data = {'repository_version': version}
                new_data.update(data)
                return new_data
            else:
                raise serializers.ValidationError(
                    detail=_('Repository has no version available to publish'))
        raise serializers.ValidationError(
            _("Either the 'repository' or 'repository_version' need to be specified "
              "but not both.")
        )


class CookbookPackageContentViewSet(ContentViewSet):
    endpoint_name = 'cookbook'
    queryset = CookbookPackageContent.objects.all()
    serializer_class = CookbookPackageContentSerializer
    filter_class = CookbookPackageContentFilter

    @transaction.atomic
    def create(self, request):
        data = request.data
        try:
            artifact = self.get_resource(data['artifact'], Artifact)
        except KeyError:
            raise serializers.ValidationError(detail={'artifact': _('This field is required')})

        try:
            metadata = CookbookMetadata.from_cookbook_file(artifact.file.name, data['name'])
        except KeyError:
            raise serializers.ValidationError(detail={'name': _('This field is required')})
        except FileNotFoundError:
            raise serializers.ValidationError(
                detail={'artifact': _('No metadata.json found in cookbook tar')})

        try:
            if data['version'] != metadata.version:
                raise serializers.ValidationError(
                    detail={'version': _('version does not correspond to version in cookbook tar')})
        except KeyError:
            pass
        data['version'] = metadata.version
        serializer = self.get_serializer(data=data)
        serializer.is_valid(raise_exception=True)
        content = serializer.save(dependencies=metadata.dependencies)
        content.artifact = artifact

        headers = self.get_success_headers(request.data)
        return Response(serializer.data, status=status.HTTP_201_CREATED, headers=headers)


class CookbookPublisherViewSet(PublisherViewSet):
    endpoint_name = 'cookbook'
    queryset = CookbookPublisher.objects.all()
    serializer_class = CookbookPublisherSerializer

    @detail_route(methods=('post',), serializer_class=_RepositoryPublishURLSerializer)
    def publish(self, request, pk):
        """
        Publishes a repository. Either the ``repository`` or the ``repository_version`` fields can
        be provided but not both at the same time.
        """
        publisher = self.get_object()
        serializer = _RepositoryPublishURLSerializer(data=request.data,
                                                     context={'request': request})
        serializer.is_valid(raise_exception=True)
        repository_version = serializer.validated_data.get('repository_version')

        result = tasks.publish.apply_async_with_reservation(
            [repository_version.repository, publisher],
            kwargs={
                'publisher_pk': str(publisher.pk),
                'repository_version_pk': str(repository_version.pk)
            }
        )
        return OperationPostponedResponse(result, request)